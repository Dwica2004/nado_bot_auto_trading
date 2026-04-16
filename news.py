# ABOUTME: News scraper and sentiment analysis engine.
# ABOUTME: Fetches from RSS feeds (CoinDesk, CoinTelegraph) and Twitter keywords.
# ABOUTME: Returns sentiment scores: positive, negative, or neutral.

import json
import logging
import os
import re
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


# ─── Configuration ────────────────────────────────────────────────────────────

def _load_news_config() -> dict:
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f).get("news", {})


NEWS_CFG = _load_news_config()

POSITIVE_KEYWORDS = [kw.lower() for kw in NEWS_CFG.get("positive_keywords", [
    "bullish", "breakout", "listing", "partnership", "launch",
    "rally", "surge", "pump", "moon", "adoption", "approved",
])]

NEGATIVE_KEYWORDS = [kw.lower() for kw in NEWS_CFG.get("negative_keywords", [
    "hack", "exploit", "dump", "bearish", "selloff",
    "crash", "scam", "fraud", "liquidation", "ban",
])]

RSS_FEEDS = NEWS_CFG.get("rss_feeds", [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
])

TWITTER_KEYWORDS = NEWS_CFG.get("twitter_keywords", [
    "BTC", "ETH", "crypto", "listing", "partnership", "bitcoin", "ethereum",
])

SCRAPE_INTERVAL = NEWS_CFG.get("scrape_interval_seconds", 45)


# ─── Headline Data Model ──────────────────────────────────────────────────────

@dataclass
class Headline:
    """A single news headline with metadata."""
    title: str
    source: str
    url: str = ""
    published: Optional[datetime] = None
    sentiment_score: float = 0.0
    sentiment_label: str = "neutral"

    def __repr__(self):
        ts = self.published.strftime("%H:%M:%S") if self.published else "N/A"
        return f"[{ts}] [{self.sentiment_label.upper():>8}] ({self.source}) {self.title[:80]}"


# ─── Sentiment Analyzer ───────────────────────────────────────────────────────

class SentimentAnalyzer:
    """
    Simple keyword-based sentiment scoring.

    Scoring:
      - Each positive keyword found in text → +1
      - Each negative keyword found in text → -1
      - Final score > 0 → "positive"
      - Final score < 0 → "negative"
      - Final score == 0 → "neutral"
    """

    def __init__(
        self,
        positive_words: List[str] = None,
        negative_words: List[str] = None,
    ):
        self.positive_words = positive_words or POSITIVE_KEYWORDS
        self.negative_words = negative_words or NEGATIVE_KEYWORDS

    def analyze(self, text: str) -> Tuple[str, float]:
        """
        Analyze text for sentiment.
        Returns: (label, score) where label is "positive", "negative", or "neutral"
        """
        text_lower = text.lower()
        score = 0.0

        for word in self.positive_words:
            if word in text_lower:
                score += 1.0

        for word in self.negative_words:
            if word in text_lower:
                score -= 1.0

        if score > 0:
            return "positive", score
        elif score < 0:
            return "negative", score
        else:
            return "neutral", 0.0

    def analyze_headlines(self, headlines: List[Headline]) -> Tuple[str, float]:
        """
        Analyze a batch of headlines and return aggregate sentiment.
        """
        if not headlines:
            return "neutral", 0.0

        total_score = 0.0
        for h in headlines:
            label, score = self.analyze(h.title)
            h.sentiment_label = label
            h.sentiment_score = score
            total_score += score

        avg_score = total_score / len(headlines)

        if avg_score > 0.3:
            return "positive", avg_score
        elif avg_score < -0.3:
            return "negative", avg_score
        else:
            return "neutral", avg_score


# ─── RSS Feed Scraper ─────────────────────────────────────────────────────────

class RSSFeedScraper:
    """Fetches and parses RSS feeds from crypto news sources."""

    def __init__(self, feed_urls: List[str] = None):
        self.feed_urls = feed_urls or RSS_FEEDS
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "NadoAutoTrader/1.0 (RSS Reader)",
            "Accept": "application/rss+xml, application/xml, text/xml",
        })

    def fetch_all(self, max_per_feed: int = 10) -> List[Headline]:
        """Fetch headlines from all configured RSS feeds."""
        headlines = []

        for feed_url in self.feed_urls:
            try:
                fetched = self._fetch_feed(feed_url, max_per_feed)
                headlines.extend(fetched)
            except Exception as e:
                logger.warning(f"RSS fetch failed for {feed_url}: {e}")

        # Sort by publication date (newest first)
        headlines.sort(key=lambda h: h.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return headlines

    def _fetch_feed(self, url: str, max_items: int = 10) -> List[Headline]:
        """Fetch a single RSS feed and parse items."""
        try:
            import feedparser
        except ImportError:
            logger.warning("feedparser not installed — using fallback XML parsing")
            return self._fetch_feed_fallback(url, max_items)

        try:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            logger.warning(f"Failed to fetch RSS {url}: {e}")
            return []

        headlines = []
        source = self._extract_source(url)

        for entry in feed.entries[:max_items]:
            title = entry.get("title", "").strip()
            if not title:
                continue

            link = entry.get("link", "")
            pub_date = None

            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pass

            headlines.append(Headline(
                title=title,
                source=source,
                url=link,
                published=pub_date,
            ))

        return headlines

    def _fetch_feed_fallback(self, url: str, max_items: int = 10) -> List[Headline]:
        """Fallback XML parsing when feedparser is not available."""
        try:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            content = resp.text
        except Exception as e:
            logger.warning(f"Failed to fetch RSS (fallback) {url}: {e}")
            return []

        headlines = []
        source = self._extract_source(url)

        # Simple regex extraction of <title> tags within <item> blocks
        items = re.findall(r"<item>(.*?)</item>", content, re.DOTALL)
        for item_xml in items[:max_items]:
            title_match = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item_xml)
            link_match = re.search(r"<link>(.*?)</link>", item_xml)

            if title_match:
                title = title_match.group(1).strip()
                link = link_match.group(1).strip() if link_match else ""
                headlines.append(Headline(
                    title=title,
                    source=source,
                    url=link,
                    published=datetime.now(timezone.utc),
                ))

        return headlines

    @staticmethod
    def _extract_source(url: str) -> str:
        """Extract a human-readable source name from URL."""
        domain = re.search(r"(?:https?://)?(?:www\.)?([^/]+)", url)
        if domain:
            parts = domain.group(1).split(".")
            if len(parts) >= 2:
                return parts[-2].capitalize()
        return "Unknown"


# ─── Twitter/X Scraper ────────────────────────────────────────────────────────

class TwitterScraper:
    """
    Scrapes crypto-related tweets/posts.

    NOTE: Uses the Nitter public instances as a fallback since the official
    Twitter/X API requires paid access. Falls back to keyword web search
    if Nitter is unavailable.
    """

    NITTER_INSTANCES = [
        "https://nitter.net",
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
    ]

    def __init__(self, keywords: List[str] = None):
        self.keywords = keywords or TWITTER_KEYWORDS
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })

    def fetch_tweets(self, max_results: int = 20) -> List[Headline]:
        """
        Attempt to scrape recent crypto tweets.
        Falls back to a simulated sentiment from crypto news APIs if scraping fails.
        """
        headlines = []

        # Try Nitter scraping first
        for instance in self.NITTER_INSTANCES:
            try:
                results = self._scrape_nitter(instance, max_results)
                if results:
                    headlines.extend(results)
                    break
            except Exception as e:
                logger.debug(f"Nitter {instance} failed: {e}")
                continue

        # If no Nitter results, try CryptoCompare news API (free, no key required)
        if not headlines:
            headlines = self._fetch_cryptocompare_news(max_results)

        return headlines

    def _scrape_nitter(self, instance: str, max_results: int) -> List[Headline]:
        """Scrape Nitter search for crypto keywords."""
        headlines = []
        query = " OR ".join(self.keywords[:5])  # Limit query length
        url = f"{instance}/search?q={requests.utils.quote(query)}&f=tweets"

        try:
            resp = self._session.get(url, timeout=10)
            if resp.status_code != 200:
                return []

            # Extract tweet text from HTML
            tweet_texts = re.findall(
                r'<div class="tweet-content[^"]*">(.*?)</div>',
                resp.text, re.DOTALL
            )

            for text in tweet_texts[:max_results]:
                clean_text = re.sub(r"<[^>]+>", "", text).strip()
                if clean_text and len(clean_text) > 10:
                    headlines.append(Headline(
                        title=clean_text[:200],
                        source="Twitter",
                        published=datetime.now(timezone.utc),
                    ))

        except Exception as e:
            logger.debug(f"Nitter scrape error: {e}")

        return headlines

    def _fetch_cryptocompare_news(self, max_results: int) -> List[Headline]:
        """Fallback: Fetch from CryptoCompare news API (free tier)."""
        headlines = []
        try:
            url = "https://min-api.cryptocompare.com/data/v2/news/?lang=EN"
            resp = self._session.get(url, timeout=10)
            if resp.status_code != 200:
                return []

            data = resp.json()
            for item in data.get("Data", [])[:max_results]:
                title = item.get("title", "").strip()
                if title:
                    pub_ts = item.get("published_on", 0)
                    pub_date = datetime.fromtimestamp(pub_ts, tz=timezone.utc) if pub_ts else None
                    headlines.append(Headline(
                        title=title,
                        source="CryptoCompare",
                        url=item.get("url", ""),
                        published=pub_date,
                    ))

        except Exception as e:
            logger.debug(f"CryptoCompare fetch error: {e}")

        return headlines


# ─── News Aggregator (Main Interface) ─────────────────────────────────────────

class NewsAggregator:
    """
    Combines all news sources and runs periodic scraping.
    Thread-safe for concurrent access from the trading loop.
    """

    def __init__(self):
        self.rss_scraper = RSSFeedScraper()
        self.twitter_scraper = TwitterScraper()
        self.sentiment_analyzer = SentimentAnalyzer()

        self._headlines: deque = deque(maxlen=200)
        self._lock = threading.Lock()
        self._last_scrape = 0.0
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Cached sentiment
        self._current_sentiment: str = "neutral"
        self._current_score: float = 0.0

    @property
    def sentiment(self) -> str:
        """Current aggregate sentiment: 'positive', 'negative', or 'neutral'."""
        return self._current_sentiment

    @property
    def sentiment_score(self) -> float:
        """Current aggregate sentiment score."""
        return self._current_score

    def get_recent_headlines(self, count: int = 10) -> List[Headline]:
        """Return the N most recent headlines."""
        with self._lock:
            return list(self._headlines)[:count]

    def start_background(self):
        """Start background scraping thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._scrape_loop, daemon=True, name="news-scraper")
        self._thread.start()
        logger.info("News aggregator started (background thread)")

    def stop(self):
        """Stop background scraping."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("News aggregator stopped")

    def scrape_now(self) -> Tuple[str, float]:
        """
        Perform an immediate scrape and return (sentiment, score).
        Thread-safe.
        """
        all_headlines = []

        # RSS feeds
        try:
            rss_headlines = self.rss_scraper.fetch_all(max_per_feed=8)
            all_headlines.extend(rss_headlines)
            logger.info(f"RSS: fetched {len(rss_headlines)} headlines")
        except Exception as e:
            logger.warning(f"RSS scrape failed: {e}")

        # Twitter/social
        try:
            twitter_headlines = self.twitter_scraper.fetch_tweets(max_results=15)
            all_headlines.extend(twitter_headlines)
            logger.info(f"Social: fetched {len(twitter_headlines)} headlines")
        except Exception as e:
            logger.warning(f"Twitter scrape failed: {e}")

        # Analyze sentiment
        sentiment, score = self.sentiment_analyzer.analyze_headlines(all_headlines)

        # Update cache (thread-safe)
        with self._lock:
            for h in all_headlines:
                # Avoid duplicates
                existing_titles = {hh.title for hh in self._headlines}
                if h.title not in existing_titles:
                    self._headlines.appendleft(h)

            self._current_sentiment = sentiment
            self._current_score = score
            self._last_scrape = time.time()

        # Log summary
        pos = sum(1 for h in all_headlines if h.sentiment_label == "positive")
        neg = sum(1 for h in all_headlines if h.sentiment_label == "negative")
        neu = sum(1 for h in all_headlines if h.sentiment_label == "neutral")
        logger.info(
            f"Sentiment: {sentiment.upper()} (score={score:+.2f}) | "
            f"pos={pos} neg={neg} neu={neu} | total={len(all_headlines)}"
        )

        return sentiment, score

    def _scrape_loop(self):
        """Background loop that scrapes every SCRAPE_INTERVAL seconds."""
        while self._running:
            try:
                self.scrape_now()
            except Exception as e:
                logger.error(f"News scrape loop error: {e}")

            # Wait with interruptible sleep
            for _ in range(int(SCRAPE_INTERVAL)):
                if not self._running:
                    break
                time.sleep(1)


# ─── Module-level convenience ─────────────────────────────────────────────────

def get_sentiment(headlines: List[Headline] = None) -> Tuple[str, float]:
    """Quick one-shot sentiment analysis."""
    analyzer = SentimentAnalyzer()
    if headlines:
        return analyzer.analyze_headlines(headlines)
    return "neutral", 0.0


if __name__ == "__main__":
    # Quick test of news scraping
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    agg = NewsAggregator()
    sentiment, score = agg.scrape_now()
    print(f"\nOverall Sentiment: {sentiment} (score: {score:+.2f})\n")

    for h in agg.get_recent_headlines(15):
        print(h)
