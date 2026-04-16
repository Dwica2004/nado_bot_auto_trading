# -*- coding: utf-8 -*-
"""
Nado Multi-Pair Scanner Bot v2
- Scan SEMUA perp coin di Nado
- Score pakai RSI + EMA + Volume + Momentum
- Entry pair terbaik (LONG/SHORT)
- Smart exit: trailing stop, break-even, TP1/TP2, early RSI exit
Run: python bot_scanner.py
"""
import asyncio, sys, time, logging, os
sys.stdout.reconfigure(encoding="utf-8")

import yfinance as yf
import pandas as pd
import numpy as np
from eth_account import Account
from eth_account.messages import encode_typed_data

from nado_api import (
    NadoRestClient,
    PRIVATE_KEY, WALLET_ADDRESS, SUBACCOUNT,
    GATEWAY, CHAIN_ID, build_sender_hex, build_sender,
    gen_order_verifying_contract,
    EIP712_DOMAIN_TEMPLATE, ORDER_TYPES,
    generate_nonce,
)
from news import NewsAggregator

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scanner.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("scanner")

# ── Config ────────────────────────────────────────────────────────────────────
SCAN_INTERVAL       = 60    # detik antar scan
RISK_PCT            = 0.25  # 25% saldo per trade
MIN_SCORE           = 3     # ✅ UPGRADED: min score 3/7 (kualitas lebih baik)
MAX_POSITIONS       = 2     # jumlah posisi simultan maksimal

# Software Isolated Margin
ISOLATED_MARGIN_PCT = 0.15  # 15% saldo per posisi
ISOLATED_MAX_LEV    = 20    # max leverage software
MIN_NOTIONAL_USD    = 100   # minimum notional Nado ($100)

# ── Scalping Mode ─────────────────────────────────────────────────────────────
# TP/SL lebih ketat untuk scalping cepat
SCALP_TP1_ATR       = 0.8   # TP1 di 0.8x ATR (ambil 50% profit cepat)
SCALP_TP2_ATR       = 1.5   # TP2 di 1.5x ATR (close semua)
SCALP_SL_ATR        = 0.8   # SL di 0.8x ATR (tight stop)
SCALP_TRAIL_PCT     = 0.008 # Trailing SL geser setiap 0.8% profit
SCALP_BE_PCT        = 0.005 # Break-even aktif saat profit +0.5%
SCALP_EARLY_RSI_L   = 70    # Early exit LONG jika RSI > 70
SCALP_EARLY_RSI_S   = 30    # Early exit SHORT jika RSI < 30

# ── Whitelist Pair (scalping: hanya pair liquid) ───────────────────────────────
# Set kosong = scan semua; Set berisi = hanya scan pair ini
WHITELIST_PAIRS = {"BTC", "ETH", "SOL", "XRP"}

# Coin yang di-skip (tidak ada di yfinance atau market blocked)
SKIP_SYMBOLS = {
    "KPEPE", "KBONK", "WLFI", "USELESS", "PUMP", "SKR",
    "VIRTUAL", "BERA", "PENGU", "HYPE", "SUI", "UNI",
    "TAO", "MSFT", "AMZN", "AAPL", "TSLA", "NVDA",
    "GOOGL", "QQQ", "SPY", "XAG", "WTI", "USDJPY",
    "EURUSD", "GBPUSD", "LIT", "ZRO", "ASTER",
    "ADA",   # market blocked di Nado
}

# Coin prioritas scan duluan
PRIORITY = ["BTC", "ETH", "SOL", "XRP"]

# News scraper interval
NEWS_REFRESH_INTERVAL = 300  # detik


# ── Math Helpers ──────────────────────────────────────────────────────────────

def ceil_tick(val: int, tick: int) -> int:
    return ((val + tick - 1) // tick) * tick if tick > 0 else val

def floor_tick(val: int, tick: int) -> int:
    return (val // tick) * tick if tick > 0 else val

def calc_rsi(closes: pd.Series, period: int = 14) -> float:
    d    = closes.diff().dropna()
    gain = d.clip(lower=0).ewm(com=period-1, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(com=period-1, adjust=False).mean()
    rs   = gain / loss.replace(0, np.nan)
    return float(100 - 100 / (1 + rs.iloc[-1]))

def calc_ema(closes: pd.Series, period: int) -> float:
    return float(closes.ewm(span=period, adjust=False).mean().iloc[-1])

def calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    hi = df["High"].tail(period)
    lo = df["Low"].tail(period)
    cl = df["Close"].shift(1).tail(period)
    tr = pd.concat([hi-lo, (hi-cl).abs(), (lo-cl).abs()], axis=1).max(axis=1)
    return float(tr.mean())

def calc_adx(df: pd.DataFrame, period: int = 14) -> float:
    """Average Directional Index — mengukur kekuatan trend (>20 = trending, <20 = ranging)."""
    try:
        hi = df["High"]; lo = df["Low"]; cl = df["Close"]
        plus_dm  = hi.diff().clip(lower=0)
        minus_dm = (-lo.diff()).clip(lower=0)
        # When plus_dm > minus_dm, use plus_dm, else 0
        plus_dm  = plus_dm.where(plus_dm > minus_dm, 0)
        minus_dm = minus_dm.where(minus_dm > plus_dm.shift(1).fillna(0), 0)
        tr = pd.concat([hi-lo, (hi-cl.shift()).abs(), (lo-cl.shift()).abs()], axis=1).max(axis=1)
        atr14   = tr.rolling(period).mean()
        plus_di  = 100 * (plus_dm.rolling(period).mean()  / atr14.replace(0, np.nan))
        minus_di = 100 * (minus_dm.rolling(period).mean() / atr14.replace(0, np.nan))
        dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        return float(dx.rolling(period).mean().iloc[-1])
    except Exception:
        return 25.0  # default: assume trending if calc fails


def score_pair(df: pd.DataFrame, price: float,
               news_sentiment: str = "neutral",
               news_score: float = 0.0,
               coin_sentiment: str = "neutral") -> dict:
    """
    Score LONG vs SHORT 0-7:
    RSI < 35 -> LONG +2  | RSI > 65 -> SHORT +2
    RSI 35-45 -> LONG +1 | RSI 55-65 -> SHORT +1
    EMA9 > EMA21 -> LONG +1 | EMA9 < EMA21 -> SHORT +1
    Vol spike + momentum -> +1
    Price vs EMA50 -> +1
    NEWS -> +/-1 or +/-2
    ADX: returned for filtering (tidak mempengaruhi score)
    """
    if len(df) < 30:
        return {"score": 0, "side": "NEUTRAL", "rsi": 50.0, "trend": "neutral",
                "atr": 0, "adx": 0, "swing_high": price, "swing_low": price}

    closes = df["Close"]
    rsi    = calc_rsi(closes, 14)
    ema9   = calc_ema(closes, 9)
    ema21  = calc_ema(closes, 21)
    ema50  = calc_ema(closes, 50) if len(df) >= 50 else ema21
    atr    = calc_atr(df, 14)
    adx    = calc_adx(df, 14)

    # Swing levels: last 20 candles
    swing_high = float(df["High"].tail(20).max())
    swing_low  = float(df["Low"].tail(20).min())

    avg_vol   = df["Volume"].rolling(20).mean().iloc[-1]
    last_vol  = df["Volume"].iloc[-1]
    vol_spike = bool(avg_vol > 0 and last_vol > avg_vol * 1.5)

    recent    = closes.iloc[-4:]
    mom_up    = recent.iloc[-1] > recent.iloc[0]
    mom_down  = recent.iloc[-1] < recent.iloc[0]

    ema_bull  = ema9 > ema21 * 1.0005
    ema_bear  = ema9 < ema21 * 0.9995
    above50   = price > ema50

    # LONG
    ls = 0
    if rsi < 35:               ls += 2
    elif rsi < 45:             ls += 1
    elif rsi > 65:             ls -= 1
    if ema_bull:               ls += 1
    if vol_spike and mom_up:   ls += 1
    if above50 and mom_up:     ls += 1

    # SHORT
    ss = 0
    if rsi > 65:               ss += 2
    elif rsi > 55:             ss += 1
    elif rsi < 35:             ss -= 1
    if ema_bear:               ss += 1
    if vol_spike and mom_down: ss += 1
    if not above50 and mom_down: ss += 1

    # ── News Sentiment Boost ──────────────────────────────────────────────────
    # Coin-specific news paling berpengaruh (+2/-2), global news lebih ringan (+1/-1)
    effective = coin_sentiment if coin_sentiment != "neutral" else news_sentiment
    weight    = 2 if coin_sentiment != "neutral" else 1

    if effective == "positive":
        ls += weight   # berita positif → boost LONG
        ss -= weight   # kurangi dorongan SHORT
    elif effective == "negative":
        ss += weight   # berita negatif → boost SHORT
        ls -= weight   # kurangi dorongan LONG

    trend = "bullish" if ema_bull else ("bearish" if ema_bear else "neutral")

    base_result = {"rsi": rsi, "trend": trend, "atr": atr,
                   "adx": adx, "swing_high": swing_high, "swing_low": swing_low}
    if ls > ss:
        return {"score": ls, "side": "LONG",  **base_result}
    elif ss > ls:
        return {"score": ss, "side": "SHORT", **base_result}
    else:
        side = "LONG" if ema_bull else ("SHORT" if ema_bear else "NEUTRAL")
        return {"score": ls, "side": side,    **base_result}


# ── Order Execution ───────────────────────────────────────────────────────────

async def place_order_raw(rest: NadoRestClient, pid: int,
                          price_x18: int, amount_x18: int,
                          appendix: int = 1,
                          expiry_seconds: int = 300) -> dict:
    expiration   = int(time.time()) + expiry_seconds

    sender_bytes = build_sender(WALLET_ADDRESS, SUBACCOUNT)
    sender_hex   = "0x" + sender_bytes.hex()

    domain = {
        **EIP712_DOMAIN_TEMPLATE,
        "chainId": CHAIN_ID,
        "verifyingContract": gen_order_verifying_contract(pid),
    }
    message = {
        "sender":     sender_bytes,
        "priceX18":   price_x18,
        "amount":     amount_x18,
        "expiration": expiration,
        "nonce":      nonce,
        "appendix":   appendix,
    }
    structured = {"types": ORDER_TYPES, "primaryType": "Order",
                  "domain": domain, "message": message}

    account  = Account.from_key(PRIVATE_KEY)
    signable = encode_typed_data(full_message=structured)
    signed   = account.sign_message(signable)

    payload = {
        "place_order": {
            "product_id": pid,
            "order": {
                "sender":     sender_hex,
                "priceX18":   str(price_x18),
                "amount":     str(amount_x18),
                "expiration": str(expiration),
                "nonce":      str(nonce),
                "appendix":   str(appendix),
            },
            "signature": "0x" + signed.signature.hex(),
            "id": nonce % 100000,
        }
    }
    return await rest.place_order(payload)


# ── Scanner Bot ───────────────────────────────────────────────────────────────

class ScannerBot:
    def __init__(self):
        self.rest      = NadoRestClient()
        self.products  = {}    # pid -> product info dict
        self.positions = {}    # pid -> open position dict (max MAX_POSITIONS)
        self.sender    = build_sender_hex(WALLET_ADDRESS, SUBACCOUNT)

        # News aggregator — berjalan di background thread
        self.news      = NewsAggregator()
        self.news.start_background()
        log.info("📰 News aggregator started (RSS + CryptoCompare)")

    # ── Product Loading ───────────────────────────────────────────────────────

    async def load_products(self):
        log.info("Loading Nado products...")
        all_p   = await self.rest.query({"type": "all_products"})
        sym_res = await self.rest.query({"type": "symbols"})
        sym_map = {v.get("product_id"): v.get("symbol", "")
                   for k, v in sym_res.get("symbols", {}).items()}

        self.products = {}
        for p in all_p.get("perp_products", []):
            pid  = p.get("product_id")
            sym  = sym_map.get(pid, "")
            if not sym.endswith("-PERP"):
                continue
            base = sym.replace("-PERP", "")
            if base in SKIP_SYMBOLS:
                continue
            # ✅ Whitelist filter: hanya scan pair yang ada di WHITELIST_PAIRS
            if WHITELIST_PAIRS and base not in WHITELIST_PAIRS:
                continue

            oracle_x18 = int(p.get("oracle_price_x18", 0))
            if oracle_x18 <= 0:
                continue

            book  = p.get("book_info", {})
            risk  = p.get("risk", {})
            lw    = float(risk.get("long_weight_initial_x18", 0)) / 1e18
            max_lev = min(round(1 / (1 - lw)) if 0 < lw < 1 else 10, 50)

            self.products[pid] = {
                "symbol":    sym,
                "base":      base,
                "yf_sym":    f"{base}-USD",
                "oracle_x18":  oracle_x18,
                "price_tick":  int(book.get("price_increment_x18", 10**18)),
                "size_tick":   int(book.get("size_increment", 50_000_000_000_000)),
                "min_size":    int(book.get("min_size", 0)),
                "max_lev":     max_lev,
            }

        log.info(f"Loaded {len(self.products)} products: "
                 f"{[v['base'] for v in self.products.values()]}")

    async def refresh_oracle_prices(self):
        all_p = await self.rest.query({"type": "all_products"})
        for p in all_p.get("perp_products", []):
            pid = p.get("product_id")
            if pid in self.products:
                self.products[pid]["oracle_x18"] = int(p.get("oracle_price_x18", 0))

    async def get_balance(self) -> float:
        return await self.rest.get_account_equity(self.sender)

    # ── Existing Position Detection ───────────────────────────────────────────

    async def check_existing_positions(self):
        """Sync posisi on-chain ke self.positions (semua posisi aktif)."""
        try:
            info  = await self.rest.query({"type": "subaccount_info",
                                           "subaccount": self.sender})
            found_any = False
            for pb in info.get("perp_balances", []):
                pid    = pb.get("product_id")
                amount = int(pb.get("balance", {}).get("amount", 0))
                if amount == 0 or pid not in self.products:
                    continue
                # Sudah ada di bot state → skip (jangan overwrite)
                if pid in self.positions:
                    continue
                found_any = True
                side   = "LONG" if amount > 0 else "SHORT"
                info_p = self.products[pid]
                price  = info_p["oracle_x18"] / 1e18
                amt    = abs(amount)
                log.info(f"  [EXISTING] {info_p['symbol']} {side} "
                         f"{amt/1e18:.4f} @ ${price:.4f}")
                # Default SL/TP: SL 3%, TP1 2%, TP2 5%
                sl  = price * (0.97 if side == "LONG" else 1.03)
                tp1 = price * (1.02 if side == "LONG" else 0.98)
                tp  = price * (1.05 if side == "LONG" else 0.95)
                self.positions[pid] = {
                    "pid": pid, "side": side, "symbol": info_p["symbol"],
                    "entry": price, "sl": sl, "tp1": tp1, "tp": tp,
                    "amount_x18": amt,
                    "price_tick": info_p["price_tick"],
                    "size_tick":  info_p["size_tick"],
                    "opened_at":  time.time(),
                    "peak_price": price,
                }
                log.info(f"  -> Track {info_p['symbol']} {side} | "
                         f"SL=${sl:.4f} TP1=${tp1:.4f} TP=${tp:.4f}")
            if not found_any and not self.positions:
                log.info("[EXISTING] No open positions")
        except Exception as e:
            log.warning(f"check_existing_positions: {e}")

    # ── Scanning ──────────────────────────────────────────────────────────────

    async def _has_open_position_on_chain(self) -> bool:
        """Query Nado API secara langsung — apakah ada posisi perp yang masih aktif."""
        try:
            info = await self.rest.query({"type": "subaccount_info",
                                          "subaccount": self.sender})
            for pb in info.get("perp_balances", []):
                amount = int(pb.get("balance", {}).get("amount", 0))
                if amount != 0:
                    return True
        except Exception:
            pass
        return False

    async def scan_and_entry(self, balance: float):
        slots_free = MAX_POSITIONS - len(self.positions)
        if slots_free <= 0:
            log.info(f"⛔ Posisi penuh ({len(self.positions)}/{MAX_POSITIONS}). Skip scan.")
            return

        log.info(f"{'-'*20} SCAN ALL {len(self.products)} COINS (slot {len(self.positions)}/{MAX_POSITIONS}) {'-'*20}")

        # ── Sync posisi on-chain yang belum tercatat ───────────────────────────
        await self.check_existing_positions()
        slots_free = MAX_POSITIONS - len(self.positions)
        if slots_free <= 0:
            log.info(f"⛔ Setelah sync: posisi penuh ({len(self.positions)}/{MAX_POSITIONS}).")
            return

        # ── Ambil sentiment global terkini ────────────────────────────────────
        global_sentiment = self.news.sentiment
        global_score     = self.news.sentiment_score
        sent_icon = "📈" if global_sentiment == "positive" else ("📉" if global_sentiment == "negative" else "📊")
        log.info(f"{sent_icon} News sentiment: {global_sentiment.upper()} (score={global_score:+.2f})")

        # Log 3 headline terbaru
        for h in self.news.get_recent_headlines(3):
            log.info(f"   📰 [{h.sentiment_label.upper():>8}] {h.title[:80]}")

        # ── Sentiment per coin dari headlines ─────────────────────────────────
        # Cek apakah ada headline yang menyebut nama coin
        def get_coin_sentiment(base: str) -> str:
            for h in self.news.get_recent_headlines(50):
                if base.lower() in h.title.lower():
                    return h.sentiment_label
            return "neutral"

        sorted_pids = sorted(
            self.products.keys(),
            key=lambda p: (0 if self.products[p]["base"] in PRIORITY else 1,
                           self.products[p]["base"])
        )
        all_syms = [self.products[p]["yf_sym"] for p in sorted_pids]

        log.info(f"Symbols: {[self.products[p]['base'] for p in sorted_pids]}")
        try:
            raw = yf.download(all_syms, period="5d", interval="5m",
                              group_by="ticker", auto_adjust=True,
                              progress=False, threads=True)
        except Exception as e:
            log.error(f"yfinance download error: {e}")
            return

        await self.refresh_oracle_prices()

        candidates = []
        for pid in sorted_pids:
            info   = self.products[pid]
            yf_sym = info["yf_sym"]
            try:
                df    = raw[yf_sym] if len(all_syms) > 1 else raw
                if df is None or df.empty or len(df) < 25:
                    continue
                price = float(df["Close"].iloc[-1])
                if not np.isfinite(price) or price <= 0:
                    continue
            except Exception:
                continue

            # ── News: cek sentiment khusus untuk coin ini ─────────────────────
            coin_sent = get_coin_sentiment(info["base"])

            result = score_pair(df, price,
                                news_sentiment=global_sentiment,
                                news_score=global_score,
                                coin_sentiment=coin_sent)

            news_tag = f" [NEWS:{coin_sent.upper()}]" if coin_sent != "neutral" else ""
            icon   = "🟢" if result["side"] == "LONG" else ("🔴" if result["side"] == "SHORT" else "⚪")
            log.info(f"  {icon} {info['base']:8s} | {result['side']:5s} "
                     f"score={result['score']:+d}/7 | RSI={result['rsi']:.1f} | "
                     f"{result['trend']}{news_tag}")

            if result["score"] >= MIN_SCORE and result["side"] != "NEUTRAL":
                # Skip coin yang sudah ada posisinya
                if pid in self.positions:
                    continue
                # ADX filter: skip ranging market (ADX < 18)
                adx_val = result.get("adx", 25)
                if adx_val < 18:
                    log.info(f"  ⏭  {info['base']:8s} | SKIP — ADX={adx_val:.1f} (ranging market)")
                    continue
                candidates.append({**result, "pid": pid, "df": df, "info": info,
                                   "coin_sent": coin_sent})

        if not candidates:
            log.info("No signal this cycle.")
            return

        # Ambil top-N sinyal terkuat sesuai slot kosong
        candidates.sort(key=lambda x: x["score"], reverse=True)
        top_picks = candidates[:slots_free]
        log.info(f"  Top picks: {[c['info']['base']+' '+c['side'] for c in top_picks]}") 

        for best in top_picks:
            await self._entry_one(best, balance, global_sentiment, global_score)

    async def _entry_one(self, best: dict, balance: float,
                         global_sentiment: str, global_score: float):
        """Place satu order entry untuk sinyal terpilih."""
        # ── Build order ──────────────────────────────────────────────────────
        pid    = best["pid"]
        info   = best["info"]
        side   = best["side"]
        lev    = info["max_lev"]
        atr    = best["atr"]

        await self.refresh_oracle_prices()
        oracle_x18  = info["oracle_x18"]
        price_tick  = info["price_tick"]
        size_tick   = info["size_tick"]
        min_size    = info["min_size"]

        oracle_rounded = floor_tick(oracle_x18, price_tick)
        entry_price    = oracle_rounded / 1e18

        # ── Software Isolated Margin sizing ───────────────────────────────────
        # isolated_margin = uang yang BENAR-BENAR kita pertaruhkan untuk posisi ini
        # Max loss = isolated_margin (bukan seluruh saldo)
        isolated_margin = balance * ISOLATED_MARGIN_PCT
        log.info(f"    [ISOLATED] Allocated margin: ${isolated_margin:.4f} USDT")

        # Notional = isolated_margin * leverage (capped Nado max)
        eff_lev        = min(lev, ISOLATED_MAX_LEV)
        notional_usd   = isolated_margin * eff_lev

        # Pastikan notional >= $100 (Nado minimum)
        if notional_usd < MIN_NOTIONAL_USD:
            # Paksa leverage lebih tinggi untuk capai minimum
            eff_lev      = int(MIN_NOTIONAL_USD / isolated_margin) + 1
            eff_lev      = min(eff_lev, lev)  # jangan melebihi Nado max
            notional_usd = isolated_margin * eff_lev
            log.info(f"    [ISOLATED] Bumped lev to {eff_lev}x for min notional")

        amount_x18_raw = int(notional_usd / entry_price * 1e18)
        amount_x18     = ceil_tick(amount_x18_raw, size_tick)

        # Bump up to meet min notional (size tick rounding may reduce slightly)
        if min_size > 0:
            check = (amount_x18 * oracle_rounded) // 10**18
            if check < min_size:
                amount_x18 = ceil_tick(
                    int(min_size * 10**18 / oracle_rounded) + 1, size_tick
                )

        amount_abs = amount_x18 / 1e18
        actual_not = amount_abs * entry_price
        actual_lev = actual_not / isolated_margin


        # ── SL dari isolated margin budget (bukan hanya ATR) ────────────────────
        # sl_distance = max_loss / size → max loss TEPAT = isolated_margin
        sl_distance_isolated = isolated_margin / amount_abs
        sl_distance_atr      = max(1.5 * atr, entry_price * 0.015)
        # Pakai LEBIH KETAT agar tidak terlalu jauh
        final_sl_dist = min(sl_distance_isolated, sl_distance_atr)

        direction  = 1 if side == "LONG" else -1
        signed_amt = amount_x18 * direction

        # ── Smart SL: Swing High/Low + ATR buffer ────────────────────────────
        swing_high = best.get("swing_high", entry_price * 1.02)
        swing_low  = best.get("swing_low",  entry_price * 0.98)

        if side == "LONG":
            # SL = swing low - 0.1% buffer (dibawah support)
            raw_sl   = swing_low * 0.999
            sl_dist  = entry_price - raw_sl
            # Clamp: min 0.5x ATR (tidak terlalu tight), max 2.0x ATR (tidak terlalu wide)
            sl_dist  = min(max(sl_dist, 0.5 * atr), 2.0 * atr)
            # Also cap at 2% of price
            sl_dist  = min(sl_dist, entry_price * 0.02)
            sl  = entry_price - sl_dist
            # TP dengan R:R minimum 1:1.2 (TP1) dan 1:2 (TP2)
            tp1 = entry_price + sl_dist * 1.2
            tp  = entry_price + sl_dist * 2.0
        else:
            # SL = swing high + 0.1% buffer (di atas resistance)
            raw_sl   = swing_high * 1.001
            sl_dist  = raw_sl - entry_price
            sl_dist  = min(max(sl_dist, 0.5 * atr), 2.0 * atr)
            sl_dist  = min(sl_dist, entry_price * 0.02)
            sl  = entry_price + sl_dist
            tp1 = entry_price - sl_dist * 1.2
            tp  = entry_price - sl_dist * 2.0

        rr_ratio = 2.0  # R:R selalu 1:2

        log.info(f"\n>>> SIGNAL: {info['symbol']} {side} | "
                 f"score={best['score']}/7 | RSI={best['rsi']:.1f} | ADX={best.get('adx',0):.1f} | "
                 f"lev={actual_lev:.1f}x [ISOLATED ${isolated_margin:.2f}]")
        if best.get("coin_sent", "neutral") != "neutral":
            log.info(f"    Coin news: {best['coin_sent'].upper()}")
        log.info(f"    Global news: {global_sentiment.upper()} ({global_score:+.2f})")
        log.info(f"    {amount_abs:.4f} {info['base']} @ ${entry_price:.4f} "
                 f"| notional ${actual_not:.2f} | margin ${isolated_margin:.2f}")
        log.info(f"    SL=${sl:.4f} ({((sl-entry_price)/entry_price*100):+.2f}%) [Swing] | "
                 f"TP1=${tp1:.4f} ({((tp1-entry_price)/entry_price*100):+.2f}%) | "
                 f"TP2=${tp:.4f} ({((tp-entry_price)/entry_price*100):+.2f}%) | R:R=1:{rr_ratio:.1f}")
        log.info(f"    Max loss = ${isolated_margin:.2f} | ATR={atr:.4f} | Swing SL/TP method")

        try:
            result = await place_order_raw(self.rest, pid, oracle_rounded, signed_amt)
            digest = result.get("digest", "?")
            log.info(f"    ✅ ORDER PLACED! digest={digest}")
            self.positions[pid] = {
                "pid": pid, "side": side, "symbol": info["symbol"],
                "entry": entry_price, "sl": sl, "tp1": tp1, "tp": tp,
                "amount_x18": amount_x18,
                "price_tick": price_tick,
                "size_tick":  size_tick,
                "opened_at":  time.time(),
                "peak_price": entry_price,
                "digest": digest,
                "isolated_margin": isolated_margin,
                "max_loss_usd":    isolated_margin,
            }
            log.info(f"    [ISOLATED MODE] Max loss hard-capped at ${isolated_margin:.2f} "
                     f"| Posisi aktif: {len(self.positions)}/{MAX_POSITIONS}")
            # Place TP limit order langsung ke exchange (muncul di Nado UI)
            await self._place_tp_on_exchange(pid, tp, tp1, amount_x18, side,
                                              price_tick, size_tick)
        except Exception as e:
            log.error(f"    Order failed: {e}")

    async def _place_tp_on_exchange(self, pid: int, tp: float, tp1: float,
                                     amount_x18: int, side: str,
                                     price_tick: int, size_tick: int):
        """
        Place TP limit order langsung ke Nado exchange.
        - Reduce-only (appendix bit 2 set = 4) → hanya close posisi
        - Expiry 24 jam → muncul di Nado UI
        - Untuk SHORT: BUY LIMIT di bawah (TP price low)
        - Untuk LONG:  SELL LIMIT di atas (TP price high)
        """
        try:
            close_dir = -1 if side == "LONG" else 1

            # TP1 (50%)
            half    = (amount_x18 // 2 // size_tick) * size_tick
            if half > 0:
                if side == "LONG":
                    tp1_x18 = ceil_tick(int(tp1 * 1e18), price_tick)
                else:
                    tp1_x18 = floor_tick(int(tp1 * 1e18), price_tick)
                close_half = half * close_dir
                r1 = await place_order_raw(
                    self.rest, pid, tp1_x18, close_half,
                    appendix=4, expiry_seconds=86400  # reduce-only, 24h
                )
                log.info(f"    [TP1 LIMIT] Placed @ ${tp1:.4f} | half={half/1e18:.4f} | "
                         f"digest={r1.get('digest','?')}")

            # TP2 (100% sisa)
            rest_amt = (amount_x18 // size_tick) * size_tick - half
            if rest_amt > 0:
                if side == "LONG":
                    tp2_x18 = ceil_tick(int(tp * 1e18), price_tick)
                else:
                    tp2_x18 = floor_tick(int(tp * 1e18), price_tick)
                close_rest = rest_amt * close_dir
                r2 = await place_order_raw(
                    self.rest, pid, tp2_x18, close_rest,
                    appendix=4, expiry_seconds=86400
                )
                log.info(f"    [TP2 LIMIT] Placed @ ${tp:.4f} | rest={rest_amt/1e18:.4f} | "
                         f"digest={r2.get('digest','?')}")

            log.info(f"    [TP ORDERS] Sekarang terlihat di Nado UI!")
        except Exception as e:
            log.warning(f"    [TP ORDER] Gagal place limit TP: {e} — SL/TP tetap via software")

    # ── Position Monitoring ───────────────────────────────────────────────────

    async def monitor_position(self):
        """
        Loop semua posisi aktif dan jalankan smart management:
        1. ⚠️  Warning jika rugi > 0.5%
        2. ✅ Break-even: SL → entry saat profit +1%
        3. 📈 Trailing: SL mengikuti peak − 1.5%
        4. 🎯 TP1 (50%): ambil setengah profit di TP1, sisa gratis ke TP2
        5. 🏆 TP2 (100%): close semua
        6. 📉 Early exit: RSI sangat overbought/oversold + sudah profit
        7. 🛑 SL/Trailing hit: close
        """
        if not self.positions:
            return
        log.info(f"  Monitoring {len(self.positions)} posisi aktif...")
        for pid in list(self.positions.keys()):
            pos = self.positions.get(pid)
            if pos is None:
                continue
            await self._monitor_one(pos)

    async def _monitor_one(self, pos: dict):
        """Monitor satu posisi."""
        pid   = pos["pid"]
        side  = pos["side"]

        entry = pos["entry"]

        # Harga terkini dari oracle
        try:
            all_p = await self.rest.query({"type": "all_products"})
            cur   = 0
            for p in all_p.get("perp_products", []):
                if p.get("product_id") == pid:
                    cur = int(p.get("oracle_price_x18", 0))
                    break
            price = cur / 1e18
        except Exception:
            return
        if price <= 0:
            return

        amount  = pos["amount_x18"] / 1e18
        pnl     = (price - entry) * amount if side == "LONG" else (entry - price) * amount
        pnl_pct = pnl / (entry * amount) * 100

        # ── ISOLATED MARGIN: hard force-close ────────────────────────────────
        # Kalau rugi sudah >= isolated_margin -> langsung close, tidak tunggu SL
        max_loss = pos.get("max_loss_usd", pos.get("isolated_margin", entry * amount * 0.05))
        if pnl < 0 and abs(pnl) >= max_loss:
            log.warning(
                f"  💥 [ISOLATED LIQUIDATION] {pos['symbol']} | "
                f"Loss={pnl:.4f} >= margin={max_loss:.4f} | FORCE CLOSE!"
            )
            await self._do_close(pos["pid"], pos["price_tick"],
                                 pos["amount_x18"], side, "ISOLATED MARGIN CALL")
            return

        # ── Trailing peak ─────────────────────────────────────────────────────
        if side == "LONG" and price > pos["peak_price"]:
            pos["peak_price"] = price
        elif side == "SHORT" and price < pos["peak_price"]:
            pos["peak_price"] = price

        trail_sl = (pos["peak_price"] * 0.985 if side == "LONG"
                    else pos["peak_price"] * 1.015)

        # Break-even at +0.5% (scalping: lebih cepat aktif)
        if pnl_pct >= SCALP_BE_PCT * 100 and not pos.get("at_breakeven"):
            pos["sl"]           = entry
            pos["at_breakeven"] = True
            log.info(f"  ✅ [BREAK-EVEN] {pos['symbol']} SL → entry ${entry:.4f} (profit {pnl_pct:+.2f}%)")

        # Trailing SL (geser tiap SCALP_TRAIL_PCT dari peak)
        if pos.get("at_breakeven"):
            trail_sl = (pos["peak_price"] * (1 - SCALP_TRAIL_PCT) if side == "LONG"
                        else pos["peak_price"] * (1 + SCALP_TRAIL_PCT))
            if side == "LONG":
                pos["sl"] = max(pos["sl"], trail_sl)
            else:
                pos["sl"] = min(pos["sl"], trail_sl)

        sl = pos["sl"]
        tp = pos["tp"]
        tp1 = pos.get("tp1")

        # Status
        flags  = ""
        if pos.get("at_breakeven"): flags += " [BE]"
        if pos.get("tp1_done"):     flags += " [TP1✓]"
        log.info(
            f"  📊 {pos['symbol']} {side}{flags} | "
            f"entry={entry:.4f} → {price:.4f} | "
            f"PnL={pnl:+.4f} ({pnl_pct:+.2f}%) | "
            f"SL={sl:.4f} TP={tp:.4f} | peak={pos['peak_price']:.4f}"
        )

        # ⚠️ Warning: rugi > 30% dari isolated margin
        iso_margin   = pos.get("isolated_margin", entry * amount * 0.05)
        loss_pct_iso = abs(pnl) / iso_margin * 100 if pnl < 0 else 0
        if pnl_pct < -0.3:
            log.warning(
                f"  ⚠️  RUGI {pnl_pct:.2f}% | {pos['symbol']} | "
                f"loss=${abs(pnl):.4f} / isolated_margin=${iso_margin:.2f} "
                f"({loss_pct_iso:.0f}% margin terpakai) | SL={pos['sl']:.4f}"
            )

        # 🎯 TP1: ambil 50%
        if tp1 and not pos.get("tp1_done"):
            hit_tp1 = (price >= tp1 if side == "LONG" else price <= tp1)
            if hit_tp1:
                half = pos["amount_x18"] // 2
                if half > 0:
                    log.info(f"  🎯 [TP1 50%] {pos['symbol']} @ ${price:.4f} | half={half/1e18:.4f}")
                    await self._do_close(pid, pos["price_tick"], half, side, "TP1 50%", partial=True)
                    pos["amount_x18"] -= half
                    pos["tp1_done"]    = True
                    pos["sl"]          = entry   # gratis ride ke TP2
                    pos["at_breakeven"] = True
                return

        # 🛑 SL hit
        hit_sl = (price <= sl if side == "LONG" else price >= sl)
        if hit_sl:
            label = "TRAILING STOP" if pos.get("at_breakeven") else "STOP LOSS"
            log.info(f"  🛑 [{label}] {pos['symbol']} @ ${price:.4f} | PnL={pnl:+.4f}")
            await self._do_close(pid, pos["price_tick"], pos["amount_x18"], side, label)
            return

        # 🏆 TP2 full
        hit_tp = (price >= tp if side == "LONG" else price <= tp)
        if hit_tp:
            log.info(f"  🏆 [TAKE PROFIT] {pos['symbol']} @ ${price:.4f} | PnL={pnl:+.4f}")
            await self._do_close(pid, pos["price_tick"], pos["amount_x18"], side, "TAKE PROFIT")
            return

        # Early exit (scalping): RSI extreme + sudah profit
        if pnl > 0 and time.time() - pos.get("opened_at", 0) > 180:
            try:
                info_p = self.products.get(pid, {})
                df_e   = yf.Ticker(info_p.get("yf_sym", "")).history(
                    period="1d", interval="5m")
                if not df_e.empty and len(df_e) >= 14:
                    rsi_e = calc_rsi(df_e["Close"], 14)
                    if side == "LONG" and rsi_e > SCALP_EARLY_RSI_L:
                        log.info(f"  [EARLY EXIT] RSI={rsi_e:.0f}>{SCALP_EARLY_RSI_L} | {pos['symbol']} +${pnl:.4f}")
                        await self._do_close(pid, pos["price_tick"], pos["amount_x18"],
                                             side, f"RSI OVERBOUGHT {rsi_e:.0f}")
                    elif side == "SHORT" and rsi_e < SCALP_EARLY_RSI_S:
                        log.info(f"  [EARLY EXIT] RSI={rsi_e:.0f}<{SCALP_EARLY_RSI_S} | {pos['symbol']} +${pnl:.4f}")
                        await self._do_close(pid, pos["price_tick"], pos["amount_x18"],
                                             side, f"RSI OVERSOLD {rsi_e:.0f}")
            except Exception:
                pass

    async def _do_close(self, pid: int, price_tick: int,
                        amount_x18: int, side: str, reason: str,
                        partial: bool = False):
        try:
            all_p = await self.rest.query({"type": "all_products"})
            cur = 0
            for p in all_p.get("perp_products", []):
                if p.get("product_id") == pid:
                    cur = int(p.get("oracle_price_x18", 0))
                    break
            oracle_rounded = floor_tick(cur, price_tick)
            close_dir = -1 if side == "LONG" else 1
            close_amt = amount_x18 * close_dir
            await place_order_raw(self.rest, pid, oracle_rounded, close_amt)
            log.info(f"  [{reason}] {'Partial' if partial else 'Full'} close OK")
        except Exception as e:
            log.error(f"  [{reason}] Close failed: {e}")
        finally:
            if not partial:
                self.positions.pop(pid, None)
                log.info(f"  Posisi aktif tersisa: {len(self.positions)}/{MAX_POSITIONS}")

    # ── Main Loop ─────────────────────────────────────────────────────────────

    async def run(self):
        log.info("=" * 60)
        log.info("  Nado Scalping Bot v3 - Smart SL/TP")
        log.info(f"  Wallet : {WALLET_ADDRESS}")
        log.info(f"  Gateway: {GATEWAY}")
        log.info(f"  Pairs  : {list(WHITELIST_PAIRS)}")
        log.info(f"  Mode   : ISOLATED MARGIN + EXCHANGE TP ORDER")
        log.info(f"  Margin : {ISOLATED_MARGIN_PCT*100:.0f}% per posisi | Max lev {ISOLATED_MAX_LEV}x")
        log.info(f"  Max posisi: {MAX_POSITIONS} | Min score: {MIN_SCORE}/7")
        log.info(f"  Monitor: tiap {MONITOR_INTERVAL}s | Scan: tiap {SCAN_INTERVAL}s")
        log.info(f"  SL: Swing High/Low | TP: R:R 1:2 | BE: +0.5% | Trail: 0.8%")
        log.info("=" * 60)

        await self.load_products()
        await self.check_existing_positions()

        cycle      = 0
        last_scan  = 0.0

        while True:
            now = time.time()
            try:
                # Monitor posisi setiap MONITOR_INTERVAL (15s) ─────────────────────
                if self.positions:
                    await self.monitor_position()

                # Scan entry baru setiap SCAN_INTERVAL (60s) ────────────────────────
                if now - last_scan >= SCAN_INTERVAL:
                    cycle += 1
                    log.info(f"\n{'='*45} Cycle #{cycle} {'='*45}")
                    balance = await self.get_balance()
                    log.info(f"Balance: ${balance:.4f} USDT")
                    log.info(f"  Posisi aktif: {len(self.positions)}/{MAX_POSITIONS}")

                    if balance < 1.0:
                        log.warning(f"Balance < $1 (${balance:.4f}), waiting...")
                    elif len(self.positions) < MAX_POSITIONS:
                        await self.scan_and_entry(balance)

                    last_scan = now

            except Exception as e:
                log.error(f"Loop error: {e}", exc_info=True)

            await asyncio.sleep(MONITOR_INTERVAL)

    async def stop(self):
        self.news.stop()
        await self.rest.close()


async def main():
    bot = ScannerBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())


