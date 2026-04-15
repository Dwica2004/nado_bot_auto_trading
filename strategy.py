# ABOUTME: Scalping strategy using BB + EMA + RSI + ATR + SNR + FIBO + OTL.
# ABOUTME: Aggressive "First-Hit" logic — any strategy valid = immediate entry.

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as ta
import yfinance as yf

from config import STRATEGY

logger = logging.getLogger(__name__)


class Signal(Enum):
    LONG  = "LONG"
    SHORT = "SHORT"
    NONE  = "NONE"


@dataclass
class TradeSignal:
    signal:    Signal
    entry:     float
    sl:        float   # stop loss price
    tp:        float   # take profit price
    rr:        float   # risk:reward ratio
    atr:       float
    rsi:       float
    reason:    str
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    @property
    def risk_points(self) -> float:
        return abs(self.entry - self.sl)

    @property
    def reward_points(self) -> float:
        return abs(self.tp - self.entry)


# ─── Indicator Computation ────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame, cfg=STRATEGY) -> pd.DataFrame:
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]

    df[f"ema_{cfg.ema_fast}"] = ta.ema(close, length=cfg.ema_fast)
    df[f"ema_{cfg.ema_slow}"] = ta.ema(close, length=cfg.ema_slow)

    bb = ta.bbands(close, length=cfg.bb_period, std=cfg.bb_std)
    if bb is not None:
        df["bb_upper"] = bb.iloc[:, 0]
        df["bb_mid"]   = bb.iloc[:, 1]
        df["bb_lower"] = bb.iloc[:, 2]

    df["rsi"] = ta.rsi(close, length=cfg.rsi_period)
    df["atr"] = ta.atr(high, low, close, length=cfg.atr_period)
    return df


# ─── Candle Fetcher ───────────────────────────────────────────────────────────

def bulk_fetch_candles(
    symbols: list[str],
    lookback: int = STRATEGY.candle_lookback,
) -> dict[str, dict[str, pd.DataFrame]]:
    """
    Fetch 1m OHLCV candles for multiple symbols, and resample to 1m, 3m, 5m locally.
    """
    try:
        period = "2d"
        interval = "1m"
        df = yf.download(symbols, period=period, interval=interval, group_by="ticker", auto_adjust=True, progress=False, threads=True, timeout=10)
        
        result = {}
        if df.empty:
            return result

        for sym in symbols:
            if len(symbols) == 1:
                sub_df = df
            else:
                try:
                    sub_df = df[sym]
                except KeyError:
                    continue
            
            if sub_df.empty:
                continue
            
            sub_df = sub_df[["Open", "High", "Low", "Close", "Volume"]].copy()
            sub_df.dropna(inplace=True)
            if sub_df.empty:
                continue
                
            res_map = {}
            res_map["1m"] = sub_df.tail(lookback * 5).copy()
            
            # 3m Resample — only include if non-empty
            df3 = sub_df.resample('3min').agg({"Open":"first", "High":"max", "Low":"min", "Close":"last", "Volume":"sum"}).dropna()
            df3 = df3.tail(lookback)
            if not df3.empty:
                res_map["3m"] = df3
            
            # 5m Resample — only include if non-empty
            df5 = sub_df.resample('5min').agg({"Open":"first", "High":"max", "Low":"min", "Close":"last", "Volume":"sum"}).dropna()
            df5 = df5.tail(lookback)
            if not df5.empty:
                res_map["5m"] = df5
            
            # Only add symbol if 5m data is usable
            if "5m" in res_map:
                result[sym] = res_map
                
        return result
    except Exception as e:
        logger.warning(f"Bulk yfinance fetch failed: {e}")
        return {}


# ─── Scalping Strategy ────────────────────────────────────────────────────────

class ScalpingStrategy:
    def __init__(self, cfg=STRATEGY):
        self.cfg              = cfg
        self._last_signal_ts  = {}
        self._signal_count    = {"LONG": 0, "SHORT": 0, "NONE": 0}

    def generate(
        self,
        current_price: float,
        product_id: int,
        df_map: Optional[dict[str, pd.DataFrame]] = None,
        symbol: str = "BTC-USD",
    ) -> TradeSignal:
        now = time.time()
        last_ts = self._last_signal_ts.get(product_id, 0.0)
        if now - last_ts < self.cfg.signal_cooldown:
            return TradeSignal(Signal.NONE, current_price, 0, 0, 0, 0, 0, "cooldown")

        if df_map is None or "5m" not in df_map:
            return TradeSignal(Signal.NONE, current_price, 0, 0, 0, 0, 0, "missing data")

        # Prepare base data
        df5 = compute_indicators(df_map["5m"].copy(), self.cfg)
        df5.dropna(inplace=True)
        if df5.empty:
            return TradeSignal(Signal.NONE, current_price, 0, 0, 0, 0, 0, "no valid indicators")
        
        last = df5.iloc[-1]
        atr = last.get("atr", float("nan"))
        rsi = last.get("rsi", float("nan"))

        # Guard: if base indicators are invalid, skip all strategies
        if pd.isna(atr) or pd.isna(rsi) or atr == 0:
            return TradeSignal(Signal.NONE, current_price, 0, 0, 0, 0, 0, "invalid atr/rsi")

        # ── Strategy 1: OTL (Open = Low / Open = High) ────────────────────────
        # Check OTL on 5m and 3m  — candle must have FLAT open side
        for tf in ["5m", "3m"]:
            if tf in df_map and len(df_map[tf]) >= 2:
                # Check the *previous* completed candle (not the forming one)
                c = df_map[tf].iloc[-2]
                # Long: Open == Low → bullish candle with no lower wick
                if c["Open"] > 0 and abs(c["Open"] - c["Low"]) / c["Open"] < 0.0005:
                    if current_price > c["Close"]:  # price must break above close
                        return self._package_signal(Signal.LONG, current_price, atr, rsi, f"OTL {tf} (O=L)", product_id)
                # Short: Open == High → bearish candle with no upper wick
                if c["Open"] > 0 and abs(c["Open"] - c["High"]) / c["Open"] < 0.0005:
                    if current_price < c["Close"]:  # price must break below close
                        return self._package_signal(Signal.SHORT, current_price, atr, rsi, f"OTL {tf} (O=H)", product_id)

        # ── Strategy 2: Scalping (BB + RSI) ────────────────────────────────────
        # AGGRESSIVE: No EMA trend filter — BB band touch + RSI is enough
        for tf in ["5m", "3m"]:
            if tf in df_map and not df_map[tf].empty:
                df_calc = compute_indicators(df_map[tf].copy(), self.cfg)
                df_calc.dropna(inplace=True)
                if df_calc.empty:
                    continue

                l_tf  = df_calc.iloc[-1]
                b_up   = l_tf.get("bb_upper", float("nan"))
                b_lo   = l_tf.get("bb_lower", float("nan"))
                r_tf   = l_tf.get("rsi",      float("nan"))

                if any(pd.isna(v) for v in [b_up, b_lo, r_tf]):
                    continue

                # Long: Price at/below BB lower + RSI not overbought
                if current_price <= b_lo * (1 + self.cfg.bb_entry_buffer) and r_tf < self.cfg.rsi_long_max:
                    return self._package_signal(Signal.LONG, current_price, atr, rsi, f"Scalp {tf} (BB+RSI)", product_id)

                # Short: Price at/above BB upper + RSI not oversold
                if current_price >= b_up * (1 - self.cfg.bb_entry_buffer) and r_tf > self.cfg.rsi_short_min:
                    return self._package_signal(Signal.SHORT, current_price, atr, rsi, f"Scalp {tf} (BB+RSI)", product_id)

        # ── Strategy 3: Sniper (S&R + Fibonacci) ──────────────────────────────
        # AGGRESSIVE: fibo OR support is enough, direction from price position
        hh   = df5["High"].max()
        ll   = df5["Low"].min()
        diff = hh - ll
        if diff == 0:
            self._signal_count["NONE"] += 1
            return TradeSignal(Signal.NONE, current_price, 0, 0, 0, round(float(atr), 4), round(float(rsi), 2), "zero range")

        mid = (hh + ll) / 2
        fibs = [hh, hh - 0.236*diff, hh - 0.382*diff, hh - 0.5*diff, hh - 0.618*diff, hh - 0.786*diff, ll]

        def _near(lvl: float, tol: float = 0.0025) -> bool:
            return abs(current_price - lvl) / current_price <= tol

        at_fibo = any(_near(f) for f in fibs)

        # S&R from 1m, 3m, 5m
        sr_support, sr_resist = [], []
        for t_frame in ["1m", "3m", "5m"]:
            if t_frame in df_map and not df_map[t_frame].empty:
                window = df_map[t_frame].tail(20)
                sr_support.append(window["Low"].min())
                sr_resist.append(window["High"].max())

        near_support = any(_near(v) for v in sr_support)
        near_resist  = any(_near(v) for v in sr_resist)

        # LONG: Price in lower half + near fibo or support
        if current_price < mid and (at_fibo or near_support):
            return self._package_signal(Signal.LONG, current_price, atr, rsi, "Sniper (Fibo/Support)", product_id)

        # SHORT: Price in upper half + near fibo or resistance
        if current_price > mid and (at_fibo or near_resist):
            return self._package_signal(Signal.SHORT, current_price, atr, rsi, "Sniper (Fibo/Resist)", product_id)

        self._signal_count["NONE"] += 1
        return TradeSignal(Signal.NONE, current_price, 0, 0, 0, round(float(atr), 4), round(float(rsi), 2), "no setup")

    def _package_signal(self, sig: Signal, price: float, atr: float, rsi: float, reason: str, pid: int) -> TradeSignal:
        sl_dist = self.cfg.atr_sl_mult * atr
        tp_dist = self.cfg.atr_tp_mult * atr
        
        if sig == Signal.LONG:
            sl, tp = price - sl_dist, price + tp_dist
        else:
            sl, tp = price + sl_dist, price - tp_dist
            
        self._last_signal_ts[pid] = time.time()
        self._signal_count[sig.value] += 1
        logger.info(f"🚀 AGRESSIVE {sig.value} | {reason} | price={price:.2f} | sl={sl:.2f} | tp={tp:.2f}")
        
        return TradeSignal(
            signal=sig, entry=price, sl=round(sl, 4), tp=round(tp, 4),
            rr=round(tp_dist/sl_dist, 2), atr=round(atr, 4), rsi=round(rsi, 2), reason=reason
        )

    def stats(self) -> dict:
        return {**self._signal_count, "total": sum(self._signal_count.values())}
