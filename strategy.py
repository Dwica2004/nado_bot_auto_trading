# ABOUTME: Premium entry strategy — multi-filter system for high-probability entries.
# ABOUTME: Uses RSI + EMA trend + breakout + volume + sentiment. Score-based: need ≥3/5 conditions.

import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─── Configuration ────────────────────────────────────────────────────────────

def _load_strategy_config() -> dict:
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("strategy", {}), cfg.get("risk", {})


STRATEGY_CFG, RISK_CFG = _load_strategy_config()


# ─── Enums & Data Classes ────────────────────────────────────────────────────

class Signal(Enum):
    LONG  = "LONG"
    SHORT = "SHORT"
    NONE  = "NONE"


@dataclass
class TradeSignal:
    """Complete trade signal with all entry/exit parameters."""
    signal:        Signal
    entry:         float
    sl:            float    # stop loss price
    tp:            float    # take profit price
    reason:        str
    sentiment:     str = "neutral"
    volume_ratio:  float = 0.0
    breakout_type: str = ""
    score:         int = 0
    timestamp:     float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    @property
    def risk_points(self) -> float:
        return abs(self.entry - self.sl)

    @property
    def reward_points(self) -> float:
        return abs(self.tp - self.entry)

    @property
    def risk_reward(self) -> float:
        risk = self.risk_points
        return self.reward_points / risk if risk > 0 else 0.0


NO_SIGNAL = TradeSignal(Signal.NONE, 0, 0, 0, "no signal")


# ─── Technical Indicators ─────────────────────────────────────────────────────

def calc_rsi(close: pd.Series, period: int = 14) -> float:
    """Calculate RSI. Returns latest RSI value (0-100)."""
    if len(close) < period + 1:
        return 50.0
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def calc_ema(close: pd.Series, period: int) -> float:
    """Calculate EMA. Returns latest value."""
    if len(close) < period:
        return float(close.iloc[-1])
    return float(close.ewm(span=period, adjust=False).mean().iloc[-1])


def calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Calculate Average True Range for dynamic SL/TP."""
    if len(df) < period + 1:
        return 0.0
    high = df["High"]
    low  = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(tr.ewm(span=period, adjust=False).mean().iloc[-1])


# ─── Market Analyzer ──────────────────────────────────────────────────────────

class MarketAnalyzer:
    """Multi-factor market analysis for high-probability entries."""

    def __init__(
        self,
        breakout_lookback: int = 20,
        volume_spike_mult: float = 1.8,
        rsi_oversold: float = 38.0,
        rsi_overbought: float = 62.0,
        ema_fast: int = 9,
        ema_slow: int = 21,
    ):
        self.breakout_lookback   = breakout_lookback
        self.volume_spike_mult   = volume_spike_mult
        self.rsi_oversold        = rsi_oversold
        self.rsi_overbought      = rsi_overbought
        self.ema_fast            = ema_fast
        self.ema_slow            = ema_slow

    def detect_breakout(
        self, df: pd.DataFrame, current_price: float
    ) -> Tuple[str, float, float]:
        """Detect price breakout from recent support/resistance."""
        if df is None or len(df) < self.breakout_lookback:
            return "none", 0.0, 0.0

        lookback   = df.tail(self.breakout_lookback)
        resistance = lookback["High"].max()
        support    = lookback["Low"].min()

        if current_price > resistance:
            return "resistance_break", resistance, support
        if current_price < support:
            return "support_break", resistance, support
        return "none", resistance, support

    def detect_volume_spike(self, df: pd.DataFrame) -> Tuple[bool, float]:
        """Detect if current volume is significantly above average."""
        if df is None or len(df) < self.breakout_lookback:
            return False, 0.0
        lookback    = df.tail(self.breakout_lookback)
        current_vol = lookback["Volume"].iloc[-1]
        avg_vol     = lookback["Volume"].iloc[:-1].mean()
        if avg_vol <= 0:
            return False, 0.0
        ratio    = current_vol / avg_vol
        is_spike = ratio > self.volume_spike_mult
        return is_spike, round(ratio, 2)

    def check_rsi(self, df: pd.DataFrame) -> Tuple[str, float]:
        """
        RSI filter:
          LONG  = RSI < oversold threshold (room to run up)
          SHORT = RSI > overbought threshold (room to run down)
          NONE  = neutral zone
        """
        rsi = calc_rsi(df["Close"])
        if rsi < self.rsi_oversold:
            return "oversold", rsi
        if rsi > self.rsi_overbought:
            return "overbought", rsi
        return "neutral", rsi

    def check_ema_trend(self, df: pd.DataFrame) -> str:
        """
        EMA trend filter:
          bullish = fast EMA > slow EMA (uptrend)
          bearish = fast EMA < slow EMA (downtrend)
        """
        if len(df) < self.ema_slow:
            return "neutral"
        fast = calc_ema(df["Close"], self.ema_fast)
        slow = calc_ema(df["Close"], self.ema_slow)
        if fast > slow * 1.001:    # 0.1% buffer to avoid noise
            return "bullish"
        if fast < slow * 0.999:
            return "bearish"
        return "neutral"

    def check_price_near_ema(self, df: pd.DataFrame, current_price: float) -> Tuple[bool, float]:
        """Check if price is near EMA (good entry zone, not extended)."""
        if len(df) < self.ema_slow:
            return True, 0.0
        ema = calc_ema(df["Close"], self.ema_slow)
        distance_pct = abs(current_price - ema) / ema
        # Within 2% of slow EMA = good value zone for entry
        return distance_pct < 0.02, distance_pct

    def get_atr_sl_tp(
        self,
        df: pd.DataFrame,
        current_price: float,
        signal: Signal,
        sl_atr_mult: float = 1.5,
        tp_atr_mult: float = 3.0,
    ) -> Tuple[float, float]:
        """
        Dynamic ATR-based SL/TP.
        SL = 1.5x ATR from entry
        TP = 3.0x ATR from entry (R:R = 2:1)
        """
        atr = calc_atr(df)
        if atr <= 0:
            sl_pct = RISK_CFG.get("stop_loss_pct", 0.012)
            tp_pct = RISK_CFG.get("take_profit_pct", 0.024)
            if signal == Signal.LONG:
                return current_price * (1 - sl_pct), current_price * (1 + tp_pct)
            else:
                return current_price * (1 + sl_pct), current_price * (1 - tp_pct)

        sl_dist = atr * sl_atr_mult
        tp_dist = atr * tp_atr_mult

        if signal == Signal.LONG:
            return current_price - sl_dist, current_price + tp_dist
        else:
            return current_price + sl_dist, current_price - tp_dist


# ─── Entry Strategy ───────────────────────────────────────────────────────────

class EntryStrategy:
    """
    PREMIUM STRATEGY — SCORE-BASED MULTI-FILTER
    ════════════════════════════════════════════

    SCORING SYSTEM (need ≥ 3/5 to enter):
    ┌─────────────────────────────────────────────────────┐
    │  Condition           LONG pts    SHORT pts           │
    │  ─────────────────────────────────────────────────  │
    │  1. News sentiment   positive=1  negative=1          │
    │  2. EMA trend        bullish=1   bearish=1           │
    │  3. Price breakout   resistance=1 support=1          │
    │  4. Volume spike     >1.8x avg = 1                  │
    │  5. RSI filter       <38=1       >62=1               │
    └─────────────────────────────────────────────────────┘

    RISK MANAGEMENT:
    • SL = 1.5x ATR (dynamic, adapts to volatility)
    • TP = 3.0x ATR (R:R = 2:1 minimum)
    • Risk per trade = 1% of balance (conservative for small account)
    • Cooldown = 60s per signal per product
    """

    def __init__(self):
        cfg_s = STRATEGY_CFG
        cfg_r = RISK_CFG

        self.analyzer = MarketAnalyzer(
            breakout_lookback=cfg_s.get("breakout_lookback", 20),
            volume_spike_mult=cfg_s.get("volume_spike_multiplier", 1.8),
            rsi_oversold=cfg_s.get("rsi_oversold", 38.0),
            rsi_overbought=cfg_s.get("rsi_overbought", 62.0),
            ema_fast=cfg_s.get("ema_fast", 9),
            ema_slow=cfg_s.get("ema_slow", 21),
        )

        self.min_score       = cfg_s.get("min_entry_score", 3)
        self.use_sentiment   = cfg_s.get("sentiment_weight", True)
        self.min_rr          = cfg_s.get("min_risk_reward", 1.8)

        # Per-product cooldown tracking
        self._last_signal_ts: Dict[int, float] = {}
        self._cooldown = 60  # seconds between signals per product

        # Stats
        self._total_checks  = 0
        self._total_signals = 0
        self._score_log: List[dict] = []

    def evaluate(
        self,
        product_id: int,
        current_price: float,
        candles: pd.DataFrame,
        sentiment: str,
    ) -> TradeSignal:
        """
        Score-based entry evaluation.
        Returns LONG/SHORT if score >= min_score, else NONE.
        """
        self._total_checks += 1

        # ── Cooldown Check ──────────────────────────────────────────────────────
        now     = time.time()
        last_ts = self._last_signal_ts.get(product_id, 0.0)
        if now - last_ts < self._cooldown:
            return TradeSignal(Signal.NONE, current_price, 0, 0, "cooldown active")

        # ── Validate Data ────────────────────────────────────────────────────────
        if candles is None or candles.empty or len(candles) < 25:
            return TradeSignal(Signal.NONE, current_price, 0, 0, "insufficient data (<25 candles)")

        if current_price <= 0:
            return TradeSignal(Signal.NONE, current_price, 0, 0, "invalid price")

        # ── Run All Filters ──────────────────────────────────────────────────────
        breakout_type, resistance, support = self.analyzer.detect_breakout(candles, current_price)
        is_vol_spike, volume_ratio          = self.analyzer.detect_volume_spike(candles)
        rsi_state, rsi_val                   = self.analyzer.check_rsi(candles)
        ema_trend                            = self.analyzer.check_ema_trend(candles)

        # ── Score for LONG ───────────────────────────────────────────────────────
        long_score = 0
        long_reasons = []

        if sentiment == "positive":
            long_score += 1
            long_reasons.append(f"sentiment=positive")

        if ema_trend == "bullish":
            long_score += 1
            long_reasons.append(f"EMA trend=bullish")

        if breakout_type == "resistance_break":
            long_score += 1
            long_reasons.append(f"breakout above {resistance:.2f}")

        if is_vol_spike:
            long_score += 1
            long_reasons.append(f"vol={volume_ratio:.1f}x spike")

        if rsi_state == "oversold":
            long_score += 1
            long_reasons.append(f"RSI={rsi_val:.0f} oversold")

        # ── Score for SHORT ──────────────────────────────────────────────────────
        short_score = 0
        short_reasons = []

        if sentiment == "negative":
            short_score += 1
            short_reasons.append(f"sentiment=negative")

        if ema_trend == "bearish":
            short_score += 1
            short_reasons.append(f"EMA trend=bearish")

        if breakout_type == "support_break":
            short_score += 1
            short_reasons.append(f"breakdown below {support:.2f}")

        if is_vol_spike:
            short_score += 1
            short_reasons.append(f"vol={volume_ratio:.1f}x spike")

        if rsi_state == "overbought":
            short_score += 1
            short_reasons.append(f"RSI={rsi_val:.0f} overbought")

        # ── Pick Best Direction ──────────────────────────────────────────────────
        best_score = max(long_score, short_score)

        # Log score for visibility
        logger.debug(
            f"Score | LONG={long_score}/5 | SHORT={short_score}/5 | "
            f"RSI={rsi_val:.0f} | trend={ema_trend} | vol={volume_ratio:.1f}x | "
            f"breakout={breakout_type} | sentiment={sentiment}"
        )

        if best_score < self.min_score:
            return TradeSignal(
                Signal.NONE, current_price, 0, 0,
                f"score {best_score}/5 < min {self.min_score} | "
                f"LONG={long_score} SHORT={short_score}",
                sentiment=sentiment,
                volume_ratio=volume_ratio,
            )

        # ── Direction with Higher Score ──────────────────────────────────────────
        if long_score >= short_score and long_score >= self.min_score:
            direction = Signal.LONG
            reasons   = long_reasons
            score     = long_score
        elif short_score > long_score and short_score >= self.min_score:
            direction = Signal.SHORT
            reasons   = short_reasons
            score     = short_score
        else:
            return TradeSignal(
                Signal.NONE, current_price, 0, 0,
                f"tied scores, no clear direction (L={long_score} S={short_score})",
                sentiment=sentiment,
            )

        # ── Calculate ATR-Based SL/TP ────────────────────────────────────────────
        sl, tp = self.analyzer.get_atr_sl_tp(candles, current_price, direction)

        # Verify Risk:Reward ratio
        rr = abs(tp - current_price) / abs(current_price - sl) if abs(current_price - sl) > 0 else 0
        if rr < self.min_rr:
            return TradeSignal(
                Signal.NONE, current_price, 0, 0,
                f"R:R={rr:.2f} below min {self.min_rr:.1f} — skipping",
                sentiment=sentiment,
                score=score,
            )

        # ── Build Final Signal ───────────────────────────────────────────────────
        self._last_signal_ts[product_id] = now
        self._total_signals += 1

        reason = f"[{score}/5] " + " | ".join(reasons) + f" | R:R={rr:.1f}"
        logger.info(f"✅ SIGNAL [{score}/5]: {direction.value} @ {current_price:.4f} — {reason}")

        return TradeSignal(
            signal=direction,
            entry=current_price,
            sl=round(sl, 6),
            tp=round(tp, 6),
            reason=reason,
            sentiment=sentiment,
            volume_ratio=volume_ratio,
            breakout_type=breakout_type,
            score=score,
        )

    def stats(self) -> dict:
        """Return strategy evaluation statistics."""
        return {
            "total_checks":  self._total_checks,
            "total_signals": self._total_signals,
            "signal_rate":   f"{(self._total_signals/self._total_checks*100):.2f}%" if self._total_checks else "0%",
        }


# ─── Position Sizing ──────────────────────────────────────────────────────────

def calculate_position_size(
    balance: float,
    entry_price: float,
    sl_price: float,
    risk_pct: float = None,
) -> float:
    """
    Conservative position sizing for small accounts.

    Risk per trade = 1% of balance (max $0.05 on $5 account).
    Position size  = risk_amount / sl_distance_in_price
    Notional is capped at 90% of balance to prevent over-leverage.

    Args:
        balance:    Total account balance in USDT
        entry_price: Entry price
        sl_price:   Stop loss price
        risk_pct:   Override risk percentage

    Returns:
        Position size in base asset units
    """
    if risk_pct is None:
        risk_pct = RISK_CFG.get("risk_per_trade_pct", 0.01)

    risk_amount = balance * risk_pct
    sl_distance = abs(entry_price - sl_price)

    if sl_distance <= 0:
        logger.warning("SL distance = 0, cannot calculate position size")
        return 0.0

    amount  = risk_amount / sl_distance
    notional = amount * entry_price

    # Cap notional at 90% of balance (no over-leverage)
    max_notional = balance * 0.90
    if notional > max_notional:
        amount   = max_notional / entry_price
        notional = max_notional
        logger.info(f"📐 Capped to max notional {max_notional:.2f}")

    rr = abs(entry_price - sl_price) / entry_price * 100

    logger.info(
        f"📐 Position sizing | balance={balance:.2f} | risk={risk_amount:.4f} ({risk_pct:.1%}) | "
        f"sl_dist={sl_distance:.4f} ({rr:.2f}%) | "
        f"→ size={amount:.6f} | notional={notional:.4f} USDT"
    )

    return amount
