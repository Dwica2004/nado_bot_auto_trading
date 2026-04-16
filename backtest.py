# ABOUTME: Backtesting engine — simulates the trading strategy on historical data.
# ABOUTME: Tests sentiment + breakout + volume strategy against past candle data.

import json
import logging
import os
import sys
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Force UTF-8 on Windows
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)


# ─── Configuration ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


CFG = _load_config()


# ─── Simulated Sentiment Generator ────────────────────────────────────────────

class SimulatedSentiment:
    """
    Generates simulated sentiment based on price momentum.
    For backtesting only — in live mode, real news data is used.
    """

    def __init__(self, momentum_window: int = 10):
        self.momentum_window = momentum_window

    def get_sentiment(self, df: pd.DataFrame, idx: int) -> str:
        """
        Derive sentiment from price momentum:
        - If price is trending up over last N candles → "positive"
        - If price is trending down → "negative"
        - Otherwise → "neutral"
        """
        if idx < self.momentum_window:
            return "neutral"

        window = df.iloc[idx - self.momentum_window : idx]
        start_price = float(window["Close"].iloc[0])
        end_price = float(window["Close"].iloc[-1])

        pct_change = (end_price - start_price) / start_price

        if pct_change > 0.005:   # > 0.5% up
            return "positive"
        elif pct_change < -0.005:  # > 0.5% down
            return "negative"
        else:
            return "neutral"


# ─── Backtest Trade ───────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    """Record of a single backtest trade."""
    entry_time:  str
    exit_time:   str
    symbol:      str
    side:        str
    entry_price: float
    exit_price:  float
    amount:      float
    pnl:         float
    pnl_pct:     float
    reason:      str
    sentiment:   str
    duration_candles: int = 0


# ─── Backtest Engine ──────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Simulates the triple-confirmation strategy on historical OHLCV data.

    Conditions tested:
      LONG:  sentiment=positive + resistance break + volume spike > 1.5x
      SHORT: sentiment=negative + support break   + volume spike > 1.5x
    """

    def __init__(
        self,
        initial_balance: float = 1000.0,
        risk_per_trade_pct: float = 0.02,
        sl_pct: float = 0.015,
        tp_pct: float = 0.03,
        breakout_lookback: int = 20,
        volume_spike_mult: float = 1.5,
    ):
        self.initial_balance   = initial_balance
        self.balance           = initial_balance
        self.risk_per_trade    = risk_per_trade_pct
        self.sl_pct            = sl_pct
        self.tp_pct            = tp_pct
        self.breakout_lookback = breakout_lookback
        self.volume_spike_mult = volume_spike_mult

        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[float] = [initial_balance]
        self.sentiment_sim = SimulatedSentiment()

    def run(
        self,
        df: pd.DataFrame,
        symbol: str = "BTC-PERP",
    ) -> Dict:
        """
        Run backtest on a DataFrame with OHLCV columns.

        Args:
            df: DataFrame with Open, High, Low, Close, Volume columns
            symbol: Symbol name for labeling

        Returns:
            Dict with backtest results
        """
        if df is None or len(df) < self.breakout_lookback + 5:
            return {"error": "Insufficient data for backtest"}

        # Reset state
        self.balance = self.initial_balance
        self.trades = []
        self.equity_curve = [self.initial_balance]

        in_position = False
        position_side = ""
        entry_price = 0.0
        entry_idx = 0
        sl_price = 0.0
        tp_price = 0.0
        amount = 0.0
        entry_sentiment = ""

        logger.info(f"Backtest starting | {symbol} | {len(df)} candles | balance=${self.initial_balance:.2f}")

        for i in range(self.breakout_lookback, len(df)):
            row = df.iloc[i]
            current_price = float(row["Close"])
            high = float(row["High"])
            low = float(row["Low"])

            if in_position:
                # ── Check SL/TP ────────────────────────────────────────────
                hit_sl = False
                hit_tp = False

                if position_side == "LONG":
                    hit_sl = float(low) <= sl_price
                    hit_tp = float(high) >= tp_price
                else:
                    hit_sl = float(high) >= sl_price
                    hit_tp = float(low) <= tp_price

                if hit_tp:
                    exit_price = tp_price
                    pnl = (exit_price - entry_price) * amount if position_side == "LONG" \
                        else (entry_price - exit_price) * amount
                    self.balance += pnl
                    self.equity_curve.append(self.balance)

                    self.trades.append(BacktestTrade(
                        entry_time=str(df.index[entry_idx]) if hasattr(df.index[entry_idx], 'strftime') else str(entry_idx),
                        exit_time=str(df.index[i]) if hasattr(df.index[i], 'strftime') else str(i),
                        symbol=symbol,
                        side=position_side,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        amount=amount,
                        pnl=round(pnl, 4),
                        pnl_pct=round(pnl / (amount * entry_price) * 100, 2),
                        reason="TAKE_PROFIT",
                        sentiment=entry_sentiment,
                        duration_candles=i - entry_idx,
                    ))
                    in_position = False

                elif hit_sl:
                    exit_price = sl_price
                    pnl = (exit_price - entry_price) * amount if position_side == "LONG" \
                        else (entry_price - exit_price) * amount
                    self.balance += pnl
                    self.equity_curve.append(self.balance)

                    self.trades.append(BacktestTrade(
                        entry_time=str(df.index[entry_idx]) if hasattr(df.index[entry_idx], 'strftime') else str(entry_idx),
                        exit_time=str(df.index[i]) if hasattr(df.index[i], 'strftime') else str(i),
                        symbol=symbol,
                        side=position_side,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        amount=amount,
                        pnl=round(pnl, 4),
                        pnl_pct=round(pnl / (amount * entry_price) * 100, 2),
                        reason="STOP_LOSS",
                        sentiment=entry_sentiment,
                        duration_candles=i - entry_idx,
                    ))
                    in_position = False

            else:
                # ── Check Entry Conditions ─────────────────────────────────
                lookback = df.iloc[i - self.breakout_lookback : i]

                # Condition 1: Sentiment
                sentiment = self.sentiment_sim.get_sentiment(df, i)

                # Condition 2: Breakout
                resistance = float(lookback["High"].max())
                support = float(lookback["Low"].min())

                # Condition 3: Volume spike
                current_vol = float(row["Volume"])
                avg_vol = float(lookback["Volume"].mean())
                vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0.0
                volume_spike = vol_ratio > self.volume_spike_mult

                # ── LONG: positive + resistance break + volume spike
                if (sentiment == "positive"
                    and float(current_price) > float(resistance)
                    and volume_spike
                    and self.balance > 0):

                    entry_price = float(current_price)
                    sl_price = entry_price * (1 - self.sl_pct)
                    tp_price = entry_price * (1 + self.tp_pct)
                    sl_dist = abs(entry_price - sl_price)
                    risk_amt = self.balance * self.risk_per_trade
                    amount = risk_amt / sl_dist if sl_dist > 0 else 0
                    position_side = "LONG"
                    entry_idx = i
                    entry_sentiment = sentiment
                    in_position = True

                # ── SHORT: negative + support break + volume spike
                elif (sentiment == "negative"
                      and float(current_price) < float(support)
                      and volume_spike
                      and self.balance > 0):

                    entry_price = float(current_price)
                    sl_price = entry_price * (1 + self.sl_pct)
                    tp_price = entry_price * (1 - self.tp_pct)
                    sl_dist = abs(entry_price - sl_price)
                    risk_amt = self.balance * self.risk_per_trade
                    amount = risk_amt / sl_dist if sl_dist > 0 else 0
                    position_side = "SHORT"
                    entry_idx = i
                    entry_sentiment = sentiment
                    in_position = True

        # ── Generate Results ───────────────────────────────────────────────────
        results = self._compute_results(symbol, len(df))

        # Print summary
        self._print_results(results)

        return results

    def _compute_results(self, symbol: str, total_candles: int) -> Dict:
        """Compute backtest statistics."""
        wins  = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        total  = len(self.trades)

        total_pnl = sum(t.pnl for t in self.trades)
        win_rate  = len(wins) / total if total > 0 else 0.0
        avg_win   = np.mean([t.pnl for t in wins]) if wins else 0.0
        avg_loss  = np.mean([t.pnl for t in losses]) if losses else 0.0
        max_win   = max([t.pnl for t in wins], default=0.0)
        max_loss  = min([t.pnl for t in losses], default=0.0)

        # Max drawdown
        peak = self.initial_balance
        max_dd = 0.0
        for eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

        # Profit factor
        gross_profit = sum(t.pnl for t in wins)
        gross_loss   = abs(sum(t.pnl for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        return {
            "symbol":           symbol,
            "total_candles":    total_candles,
            "total_trades":     total,
            "wins":             len(wins),
            "losses":           len(losses),
            "win_rate":         f"{win_rate:.1%}",
            "total_pnl":        round(total_pnl, 4),
            "avg_win":          round(avg_win, 4),
            "avg_loss":         round(avg_loss, 4),
            "max_win":          round(max_win, 4),
            "max_loss":         round(max_loss, 4),
            "profit_factor":    round(profit_factor, 2),
            "max_drawdown":     f"{max_dd:.2%}",
            "final_balance":    round(self.balance, 2),
            "return_pct":       f"{((self.balance / self.initial_balance) - 1) * 100:.2f}%",
            "equity_curve":     self.equity_curve,
            "trades":           [
                {
                    "entry_time": t.entry_time,
                    "exit_time":  t.exit_time,
                    "side":       t.side,
                    "entry":      t.entry_price,
                    "exit":       t.exit_price,
                    "pnl":        t.pnl,
                    "reason":     t.reason,
                    "sentiment":  t.sentiment,
                }
                for t in self.trades
            ],
        }

    def _print_results(self, results: Dict):
        """Print formatted backtest results."""
        sep = "═" * 60
        print(f"\n{sep}")
        print(f"  📊 BACKTEST RESULTS — {results['symbol']}")
        print(sep)
        print(f"  Candles Tested:    {results['total_candles']}")
        print(f"  Total Trades:      {results['total_trades']}")
        print(f"  Wins / Losses:     {results['wins']} / {results['losses']}")
        print(f"  Win Rate:          {results['win_rate']}")
        print(f"  Profit Factor:     {results['profit_factor']}")
        print(f"  Max Drawdown:      {results['max_drawdown']}")
        print(f"  ─────────────────────────────────────")
        print(f"  Initial Balance:   ${self.initial_balance:.2f}")
        print(f"  Final Balance:     ${results['final_balance']:.2f}")
        print(f"  Total PnL:         ${results['total_pnl']:+.2f}")
        print(f"  Return:            {results['return_pct']}")
        print(f"  ─────────────────────────────────────")
        print(f"  Avg Win:           ${results['avg_win']:+.4f}")
        print(f"  Avg Loss:          ${results['avg_loss']:+.4f}")
        print(f"  Max Win:           ${results['max_win']:+.4f}")
        print(f"  Max Loss:          ${results['max_loss']:+.4f}")
        print(sep)

        # Print recent trades
        if results["trades"]:
            print(f"\n  📋 Trade Log (last 10):")
            print(f"  {'Side':<6} {'Entry':<10} {'Exit':<10} {'PnL':>10} {'Reason':<15} {'Sentiment'}")
            print(f"  {'─'*60}")
            for t in results["trades"][-10:]:
                pnl_icon = "✅" if t["pnl"] > 0 else "❌"
                print(
                    f"  {t['side']:<6} {t['entry']:<10.4f} {t['exit']:<10.4f} "
                    f"{pnl_icon} ${t['pnl']:>+8.4f} {t['reason']:<15} {t['sentiment']}"
                )

        print()


# ─── CLI Runner ──────────────────────────────────────────────────────────────

def run_backtest(
    symbol: str = "BTC-PERP",
    period: str = "30d",
    interval: str = "5m",
):
    """Run backtest from command line."""
    import yfinance as yf

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cfg = CFG.get("backtest", {})
    initial_balance = cfg.get("initial_balance", 1000.0)

    # Fetch historical data
    yf_symbol = symbol.replace("-PERP", "-USD")
    print(f"Fetching {yf_symbol} data ({period}, {interval})...")
    df = yf.download(yf_symbol, period=period, interval=interval, auto_adjust=True, progress=False)

    # Flatten MultiIndex columns (yfinance quirk for single ticker)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    if df.empty:
        print(f"No data returned for {yf_symbol}")
        return

    print(f"Got {len(df)} candles")

    # Run backtest
    risk_cfg = CFG.get("risk", {})
    strategy_cfg = CFG.get("strategy", {})

    engine = BacktestEngine(
        initial_balance=initial_balance,
        risk_per_trade_pct=risk_cfg.get("risk_per_trade_pct", 0.02),
        sl_pct=risk_cfg.get("stop_loss_pct", 0.015),
        tp_pct=risk_cfg.get("take_profit_pct", 0.03),
        breakout_lookback=strategy_cfg.get("breakout_lookback", 20),
        volume_spike_mult=strategy_cfg.get("volume_spike_multiplier", 1.5),
    )

    results = engine.run(df, symbol=symbol)

    # Save results
    output_file = f"backtest_{symbol.replace('-', '_').lower()}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        # Remove equity_curve from JSON (too large)
        save_results = {k: v for k, v in results.items() if k != "equity_curve"}
        json.dump(save_results, f, indent=2, default=str)
    print(f"Results saved to {output_file}")

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Nado Auto Trader — Backtest Mode")
    parser.add_argument("--symbol", default="BTC-PERP", help="Symbol to backtest (default: BTC-PERP)")
    parser.add_argument("--period", default="30d", help="Data period (default: 30d)")
    parser.add_argument("--interval", default="5m", help="Candle interval (default: 5m)")
    args = parser.parse_args()

    run_backtest(symbol=args.symbol, period=args.period, interval=args.interval)
