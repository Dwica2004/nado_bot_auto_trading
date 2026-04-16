# ABOUTME: Nado DEX API client — REST + WebSocket for market data and order execution.
# ABOUTME: Handles wallet connection, EIP-712 signing, candle caching, and order lifecycle.

import asyncio
import json
import logging
import os
import time
import struct
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict, List, Tuple

import aiohttp
import pandas as pd
from eth_account import Account
from eth_account.messages import encode_typed_data
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SCALE_18 = 10 ** 18

# ─── Configuration Loader ─────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config.json from project root."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()
NET = CFG["network"]
WALLET_CFG = CFG["wallet"]

USE_TESTNET = NET["use_testnet"]
GATEWAY = NET["endpoints"]["gateway_testnet"] if USE_TESTNET else NET["endpoints"]["gateway_mainnet"]
WS_URL  = NET["endpoints"]["ws_testnet"]      if USE_TESTNET else NET["endpoints"]["ws_mainnet"]
CHAIN_ID = NET["chain_id_testnet"]             if USE_TESTNET else NET["chain_id_mainnet"]

PRIVATE_KEY    = os.getenv(WALLET_CFG["private_key_env"], "")
WALLET_ADDRESS = os.getenv(WALLET_CFG["wallet_address_env"], "")
SUBACCOUNT     = WALLET_CFG.get("subaccount_name", "default")

HEADERS = {
    "Content-Type":    "application/json",
    "Accept-Encoding": "gzip, deflate, br",
}

# ─── EIP-712 Type Definitions ─────────────────────────────────────────────────

EIP712_DOMAIN_TEMPLATE = {
    "name": "Nado",
    "version": "0.0.1",
}

ORDER_TYPES = {
    "EIP712Domain": [
        {"name": "name",              "type": "string"},
        {"name": "version",           "type": "string"},
        {"name": "chainId",           "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "sender",     "type": "bytes32"},
        {"name": "priceX18",   "type": "int128"},
        {"name": "amount",     "type": "int128"},
        {"name": "expiration", "type": "uint64"},
        {"name": "nonce",      "type": "uint64"},
        {"name": "appendix",   "type": "uint128"},  # required per docs
    ],
}

CANCEL_TYPES = {
    "EIP712Domain": [
        {"name": "name",              "type": "string"},
        {"name": "version",           "type": "string"},
        {"name": "chainId",           "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Cancellation": [
        {"name": "sender",     "type": "bytes32"},
        {"name": "productIds", "type": "uint32[]"},
        {"name": "digests",    "type": "bytes32[]"},
        {"name": "nonce",      "type": "uint64"},
    ],
}

# ─── Sender Encoding ──────────────────────────────────────────────────────────

def build_sender(address: str, subaccount: str = "") -> bytes:
    """
    Nado sender = bytes32: address (20 bytes) + subaccount (12 bytes, null-padded).
    """
    addr_bytes = bytes.fromhex(address.lower().removeprefix("0x"))
    if len(addr_bytes) != 20:
        raise ValueError(f"Invalid address length: {len(addr_bytes)}")
    if not subaccount or not subaccount.strip():
        subaccount = "default"
    sub_bytes = subaccount.encode("ascii")[:12].ljust(12, b"\x00")
    return addr_bytes + sub_bytes


def build_sender_hex(address: str, subaccount: str = "") -> str:
    """Return sender as 0x-prefixed hex string."""
    return "0x" + build_sender(address, subaccount).hex()


def generate_nonce() -> int:
    """Generate a unique nonce based on current time (milliseconds)."""
    ts_ms = int(time.time() * 1000) + 60000
    return (ts_ms << 20) | random.randint(0, 0xFFFFF)


def gen_order_verifying_contract(product_id: int) -> str:
    """
    Per Nado docs: for place_order, verifyingContract = address(productId)
    e.g. product_id=2 -> 0x0000000000000000000000000000000000000002
    """
    be_bytes = product_id.to_bytes(20, byteorder="big", signed=False)
    return "0x" + be_bytes.hex()



def to_x18(value: float) -> int:
    """Convert float to x18 integer (1e18 precision)."""
    return int(value * SCALE_18)


def from_x18(value: int) -> float:
    """Convert x18 integer back to float."""
    return value / SCALE_18


# ─── EIP-712 Signer ───────────────────────────────────────────────────────────

class NadoSigner:
    """Handles EIP-712 signing of Nado orders and cancellations."""

    def __init__(self, private_key: str, endpoint_addr: str, chain_id: int):
        self.account  = Account.from_key(private_key)
        self.address  = self.account.address
        self.chain_id = chain_id
        self.endpoint = endpoint_addr

        self.domain_order = {
            **EIP712_DOMAIN_TEMPLATE,
            "chainId": self.chain_id,
            "verifyingContract": endpoint_addr,   # set per-order via sign_order()
        }
        self.domain_cancel = {
            **EIP712_DOMAIN_TEMPLATE,
            "chainId": self.chain_id,
            "verifyingContract": endpoint_addr,
        }
        logger.info(f"Signer initialized | address={self.address} | chain_id={chain_id}")

    def sign_order(
        self,
        product_id: int,
        price: float,
        amount: float,       # positive = BUY, negative = SELL
        expiration: int = None,
        subaccount: str = "",
        post_only: bool = False,
    ) -> dict:
        """Build and sign a place_order execute payload."""
        if expiration is None:
            expiration = int(time.time()) + 300  # 5 minutes TTL

        sender_hex   = build_sender_hex(self.address, subaccount)
        sender_bytes = build_sender(self.address, subaccount)

        price      = round(price, 1)   # round to 0.1 increment (Nado ETH/SOL tick)
        price_x18  = to_x18(price)
        amount_x18 = to_x18(amount)
        nonce      = generate_nonce()
        appendix   = 1 | (256 if post_only else 0)   # uint128

        # Per docs: verifyingContract for orders = address(productId)
        order_domain = {
            **EIP712_DOMAIN_TEMPLATE,
            "chainId": self.chain_id,
            "verifyingContract": gen_order_verifying_contract(product_id),
        }

        message = {
            "sender":     sender_bytes,
            "priceX18":   price_x18,
            "amount":     amount_x18,
            "expiration": expiration,
            "nonce":      nonce,
            "appendix":   appendix,    # must be in signed struct
        }

        structured = {
            "types":       ORDER_TYPES,
            "primaryType": "Order",
            "domain":      order_domain,
            "message":     message,
        }

        signable  = encode_typed_data(full_message=structured)
        signed    = self.account.sign_message(signable)
        signature = signed.signature.hex()

        payload = {
            "place_order": {
                "product_id": product_id,
                "order": {
                    "sender":     sender_hex,
                    "priceX18":   str(price_x18),
                    "amount":     str(amount_x18),
                    "expiration": str(expiration),
                    "nonce":      str(nonce),
                    "appendix":   str(appendix),
                },
                "signature": "0x" + signature,
                "id": nonce % 100000,
            }
        }

        logger.debug(
            f"Order signed | product={product_id} | "
            f"{'BUY' if amount > 0 else 'SELL'} {abs(amount):.6f} @ {price:.2f} | "
            f"nonce={nonce}"
        )
        return payload

    def sign_cancel(self, product_id: int, digest: str, subaccount: str = "") -> dict:
        """Sign a cancellation for a specific order digest."""
        sender_hex   = build_sender_hex(self.address, subaccount)
        sender_bytes = build_sender(self.address, subaccount)
        nonce        = generate_nonce()

        digest_bytes = bytes.fromhex(digest.removeprefix("0x"))

        message = {
            "sender":     sender_bytes,
            "productIds": [product_id],
            "digests":    [digest_bytes],
            "nonce":      nonce,
        }

        structured = {
            "types":       CANCEL_TYPES,
            "primaryType": "Cancellation",
            "domain":      self.domain_cancel,
            "message":     message,
        }

        signable  = encode_typed_data(full_message=structured)
        signed    = self.account.sign_message(signable)
        signature = signed.signature.hex()

        return {
            "cancel_orders": {
                "product_id": product_id,
                "sender":     sender_hex,
                "digests":    [digest],
                "nonce":      str(nonce),
                "signature":  "0x" + signature,
            }
        }


# ─── REST Client ──────────────────────────────────────────────────────────────

class NadoRestClient:
    """Async REST wrapper for Nado gateway queries and executes."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._retry_count = 3
        self._retry_delay = 1.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=HEADERS)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def query(self, params: dict) -> dict:
        """GET /query with url-encoded params, with retry logic."""
        for attempt in range(self._retry_count):
            try:
                session = await self._get_session()
                url = f"{GATEWAY}/query"
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status} on {url}: {text[:200]}")
                    data = await resp.json()
                    if data.get("status") != "success":
                        raise RuntimeError(f"Query failed: {data.get('error', data)}")
                    return data.get("data", {})
            except Exception as e:
                if attempt < self._retry_count - 1:
                    logger.warning(f"Query retry {attempt+1}/{self._retry_count}: {e}")
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
                else:
                    raise
    async def query_post(self, payload: dict) -> dict:
        """POST /query with JSON body — needed for array params like product_ids."""
        for attempt in range(self._retry_count):
            try:
                session = await self._get_session()
                url = f"{GATEWAY}/query"
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status} on {url}: {text[:200]}")
                    data = await resp.json()
                    if data.get("status") != "success":
                        raise RuntimeError(f"Query failed: {data.get('error', data)}")
                    return data.get("data", {})
            except Exception as e:
                if attempt < self._retry_count - 1:
                    logger.warning(f"Query POST retry {attempt+1}/{self._retry_count}: {e}")
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
                else:
                    raise


    async def execute(self, payload: dict) -> dict:
        """POST /execute with JSON payload, with retry logic."""
        for attempt in range(self._retry_count):
            try:
                session = await self._get_session()
                url = f"{GATEWAY}/execute"
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status} on {url}: {text[:200]}")
                    data = await resp.json()
                    if data.get("status") != "success":
                        raise RuntimeError(f"Execute failed: {data.get('error', data)}")
                    return data.get("data", {})
            except Exception as e:
                if attempt < self._retry_count - 1:
                    logger.warning(f"Execute retry {attempt+1}/{self._retry_count}: {e}")
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
                else:
                    raise

    # ── Convenience Methods ────────────────────────────────────────────────────

    async def get_contracts(self) -> dict:
        return await self.query({"type": "contracts"})

    async def get_all_products(self) -> dict:
        return await self.query({"type": "all_products"})

    async def get_market_prices(self) -> dict:
        return await self.query({"type": "market_prices"})

    async def get_symbols(self) -> dict:
        return await self.query({"type": "symbols"})

    async def get_market_liquidity(self, product_id: int, depth: int = 5) -> dict:
        return await self.query({"type": "market_liquidity", "product_id": product_id, "depth": depth})

    async def get_subaccount_info(self, sender: str) -> dict:
        return await self.query({"type": "subaccount_info", "subaccount": sender})

    async def get_nonce(self, address: str) -> int:
        data = await self.query({"type": "nonces", "address": address})
        return data.get("order_nonce", 0)

    async def place_order(self, payload: dict) -> dict:
        logger.info(f"Placing order → {json.dumps(payload, default=str)[:300]}")
        result = await self.execute(payload)
        logger.info(f"Order placed ✓ | result={result}")
        return result

    async def cancel_order(self, payload: dict) -> dict:
        logger.info(f"Cancelling order → {json.dumps(payload, default=str)[:300]}")
        result = await self.execute(payload)
        logger.info(f"Order cancelled ✓ | result={result}")
        return result

    # ── Market Data Helpers ────────────────────────────────────────────────────

    async def get_best_bid_ask(self, product_id: int) -> Tuple[float, float]:
        """Return (best_bid, best_ask) from the orderbook."""
        book = await self.get_market_liquidity(product_id, depth=1)
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0][0]) / 1e18 if bids else 0.0
        best_ask = float(asks[0][0]) / 1e18 if asks else float("inf")
        return best_bid, best_ask

    async def get_mid_price(self, product_id: int) -> float:
        bid, ask = await self.get_best_bid_ask(product_id)
        return (bid + ask) / 2.0

    async def get_max_withdrawable(self, sender: str, product_id: int = 0) -> float:
        """Return max withdrawable amount for a product (0 = USDT0)."""
        try:
            data = await self.query({
                "type": "max_withdrawable",
                "sender": sender,
                "product_id": product_id,
            })
            amount = data.get("max_withdrawable", "0")
            return float(amount) / 1e18
        except Exception as e:
            logger.debug(f"max_withdrawable fallback: {e}")
            return 0.0

    async def get_account_equity(self, sender: str) -> float:
        """Return account balance (USDT0). Uses max_withdrawable as primary source."""
        # Primary: max_withdrawable product_id=0 (USDT0)
        bal = await self.get_max_withdrawable(sender, product_id=0)
        if bal > 0:
            return bal

        # Fallback: subaccount_info initial health
        try:
            info = await self.get_subaccount_info(sender)
            healths = info.get("healths", [])
            if healths and info.get("exists", False):
                health = healths[0].get("assets", "0")
                return float(health) / 1e18
        except Exception as e:
            logger.warning(f"Could not fetch account equity: {e}")
        return 0.0


# ─── Candle Cache (OHLCV) ─────────────────────────────────────────────────────

@dataclass
class CandleCache:
    """
    Maintains a rolling cache of the last N candles per product.
    Fetches from yfinance for price data (Nado doesn't expose raw OHLCV REST).
    """
    max_candles: int = 50
    _cache: Dict[str, deque] = field(default_factory=dict)
    _last_fetch: Dict[str, float] = field(default_factory=dict)

    def get_candles(self, symbol: str) -> pd.DataFrame:
        """Return cached candles as a DataFrame."""
        if symbol not in self._cache or len(self._cache[symbol]) == 0:
            return pd.DataFrame()

        df = pd.DataFrame(list(self._cache[symbol]))
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    def update_candles(self, symbol: str, candles: List[dict]):
        """Add new candles to the cache, maintaining max size."""
        if symbol not in self._cache:
            self._cache[symbol] = deque(maxlen=self.max_candles)

        for candle in candles:
            self._cache[symbol].append(candle)

        self._last_fetch[symbol] = time.time()

    def needs_refresh(self, symbol: str, interval: float = 60.0) -> bool:
        """Check if candles need refreshing."""
        last = self._last_fetch.get(symbol, 0.0)
        return (time.time() - last) > interval


def fetch_candles_yfinance(
    symbols: List[str],
    period: str = "2d",
    interval: str = "5m",
    lookback: int = 50,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch OHLCV candles from yfinance for multiple symbols.
    Returns dict of symbol -> DataFrame with last `lookback` candles.
    """
    import yfinance as yf

    result = {}
    try:
        tickers = [s.replace("-PERP", "-USD") for s in symbols]
        df = yf.download(
            tickers, period=period, interval=interval,
            group_by="ticker", auto_adjust=True,
            progress=False, threads=True, timeout=15,
        )

        if df.empty:
            return result

        for sym, ticker in zip(symbols, tickers):
            try:
                if len(tickers) == 1:
                    sub_df = df
                else:
                    sub_df = df[ticker]

                if sub_df.empty:
                    continue

                sub_df = sub_df[["Open", "High", "Low", "Close", "Volume"]].copy()
                sub_df.dropna(inplace=True)

                if len(sub_df) < 5:
                    continue

                result[sym] = sub_df.tail(lookback).reset_index(drop=True)
            except (KeyError, Exception) as e:
                logger.debug(f"Candle fetch skip {sym}: {e}")
                continue

    except Exception as e:
        logger.warning(f"yfinance bulk fetch failed: {e}")

    return result
