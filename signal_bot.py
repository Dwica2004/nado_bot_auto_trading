import io
import logging
import sys
import time
from datetime import datetime
from typing import Optional, Dict, List

import numpy as np
import pandas as pd
import pandas_ta as ta
import yfinance as yf
import requests

# Force stdout to UTF-8 on Windows
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("signals.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("signal_bot")

# Dedicated Signal Log (Clean Feed)
signal_logger = logging.getLogger("active_signals")
signal_logger.setLevel(logging.INFO)
s_handler = logging.FileHandler("active_signals.log", encoding="utf-8")
s_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
signal_logger.addHandler(s_handler)

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# --- Config (v5.1: Hyper Scalp Mode — $0.50 Fast) ---
GATEWAY          = "https://gateway.prod.nado.xyz/v1"
SCAN_INTERVAL    = 30
MONITOR_INTERVAL = 5            # Monitor every 5s for extremely fast exits
MIN_SCORE        = 3            # 3/6 — Hyper Aggressive
SIGNAL_COOLDOWN  = 180          # 3 min general cooldown
SL_COOLDOWN      = 600          # 10 min after SL
TP_REPEAT_DELAY  = 15           # Only 15s after WIN — ride momentum fast!
INITIAL_BALANCE  = 30.44
TARGET_BALANCE   = 40.0
VIRTUAL_MARGIN   = 10.0         # $10 per trade
SCALP_TARGET     = 0.50         # Take $0.50 and repeat

# High-leverage pairs — smallest move needed to hit $0.50
# BTC/ETH (50x) = 0.10% move | SOL (40x) = 0.125% | BNB (20x) = 0.25%
HIGH_LEV_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "DOGE", "LINK", "ZEC"]

SKIP_SYMBOLS = {"KPEPE", "KBONK", "WLFI", "USELESS", "PUMP", "SKR", "VIRTUAL", "BERA", "PENGU"}
_last_ts: Dict[str, float] = {}
_sl_ts:   Dict[str, float] = {}  # SL cooldown
_tp_ts:   Dict[str, float] = {}  # TP repeat timer

# =============================================================================
# STRATEGY TOOLS
# =============================================================================

def find_sr_levels(df: pd.DataFrame):
    window = 14
    df['hh'] = df['High'].rolling(window=window, center=True).max()
    df['ll'] = df['Low'].rolling(window=window, center=True).min()
    
    # Extract peaks/troughs
    peaks = df[df['High'] == df['hh']]['High'].tolist()
    troughs = df[df['Low'] == df['ll']]['Low'].tolist()
    
    # Cluster levels (combine levels within 0.3% of each other)
    def cluster(levels):
        if not levels: return []
        levels.sort()
        clustered = []
        if not levels: return []
        curr = levels[0]
        count = 1
        for i in range(1, len(levels)):
            if (levels[i] - curr) / curr < 0.003:
                count += 1
            else:
                clustered.append((curr, count))
                curr = levels[i]
                count = 1
        clustered.append((curr, count))
        return [c[0] for c in clustered if c[1] >= 2] # Only Strong levels (hit 2+ times)
    
    return cluster(troughs), cluster(peaks)

def detect_candle_patterns(df: pd.DataFrame):
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    p = {"bullish_engulfing": False, "bearish_engulfing": False, "hammer": False, "star": False}
    if prev["Close"] < prev["Open"] and curr["Close"] > curr["Open"]:
        if curr["Open"] <= prev["Close"] and curr["Close"] >= prev["Open"]: p["bullish_engulfing"] = True
    if prev["Close"] > prev["Open"] and curr["Close"] < curr["Open"]:
        if curr["Open"] >= prev["Close"] and curr["Close"] <= prev["Open"]: p["bearish_engulfing"] = True
    body = abs(curr["Close"] - curr["Open"])
    if body > 0:
        hw, lw = curr["High"] - max(curr["Open"], curr["Close"]), min(curr["Open"], curr["Close"]) - curr["Low"]
        if lw > 2 * body and hw < 0.3 * body: p["hammer"] = True
        if hw > 2 * body and lw < 0.3 * body: p["star"] = True
    return p

# =============================================================================
# AI FUND MANAGER (VIRTUAL TRADER)
# =============================================================================

class AIFundManager:
    def __init__(self, initial_balance, target):
        self.initial_balance   = initial_balance
        self.current_balance   = initial_balance
        self.target_balance    = target
        self.raw_pnl           = 0.0
        self.active_trades     = []
        self.report_file       = "performance_report.md"
        self.history_count     = 0
        self.wins_today        = 0   # $0.50 bags collected
        self.losses_today      = 0
        self._init_report()

    def _init_report(self):
        with open(self.report_file, "a", encoding="utf-8") as f:
            f.write(f"\n\n# AI Managed Session: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Starting Balance: ${self.initial_balance} | Target: ${self.target_balance}\n")
            f.write("| Time | Symbol | Side | Lev | Entry | Exit | PnL% | PnL$ | Balance |\n")
            f.write("|---|---|---|---|---|---|---|---|---|\n")

    def add_trade(self, symbol, side, entry, sl, tp, lev, score):
        if any(t["symbol"] == symbol for t in self.active_trades): return
        if len(self.active_trades) >= 3: return # Max 3 trades ($30 cap)
        
        self.active_trades.append({
            "symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp": tp,
            "lev": lev, "score": score, "start_time": time.time(), 
            "pnl_pct": 0.0, "pnl_usd": 0.0, "at_breakeven": False, "at_lock_profit": False
        })

    def update(self, prices: Dict[str, float]):
        to_remove = []
        for t in self.active_trades:
            sym = t["symbol"]
            curr = prices.get(sym)
            if not curr: continue
            
            # 1. Live PnL
            dist = (curr - t["entry"]) / t["entry"] if t["side"] == "LONG" else (t["entry"] - curr) / t["entry"]
            t["pnl_pct"] = dist * t["lev"] * 100
            t["pnl_usd"] = (t["pnl_pct"] / 100) * VIRTUAL_MARGIN
            t["price_now"] = curr
            
            # === SCALP TARGET HIT: $0.50 → TAKE PROFIT & REPEAT ===
            if t["pnl_usd"] >= SCALP_TARGET:
                self.wins_today += 1
                self._log_exit(t, curr, f"SCALP_TP(#{self.wins_today})")
                # Short repeat delay (ride momentum)
                _tp_ts[sym] = time.time()
                # Clear general cooldown so re-entry is fast
                _last_ts.pop(sym, None)
                signal_logger.info(
                    f"[WIN #{self.wins_today}] {sym} | +${t['pnl_usd']:.2f} | Balance: ${self.current_balance:.2f} | REPEAT READY in {TP_REPEAT_DELAY}s"
                )
                to_remove.append(t)
                continue
            
            # === TRAILING PROTECTION ===
            # Lock Profit at +$0.33: move SL so worst case = +$0.33
            if t["pnl_usd"] >= 0.33 and not t["at_lock_profit"]:
                lock_dist = 0.033 / t["lev"]
                t["sl"] = t["entry"] * (1 + lock_dist) if t["side"] == "LONG" else t["entry"] * (1 - lock_dist)
                t["at_lock_profit"] = True
                log.info(f"  [LOCK] {sym} SL moved → locked +$0.33")
            
            # Break-Even at +$0.15
            if t["pnl_usd"] >= 0.15 and not t["at_breakeven"] and not t["at_lock_profit"]:
                t["sl"] = t["entry"]
                t["at_breakeven"] = True
                log.info(f"  [BE] {sym} SL moved → Break-Even")
            
            # === STOP LOSS ===
            hit_sl = (t["side"] == "LONG" and curr <= t["sl"]) or \
                     (t["side"] == "SHORT" and curr >= t["sl"])
            if hit_sl:
                self.losses_today += 1
                _sl_ts[sym] = time.time()  # 25-min SL cooldown
                self._log_exit(t, curr, "SL")
                signal_logger.info(
                    f"[LOSS #{self.losses_today}] {sym} | ${t['pnl_usd']:.2f} | Cooldown: 25min"
                )
                to_remove.append(t)
                continue
            
            # Hard SL/TP from score_signal levels (fallback)
            hit_tp = (t["side"] == "LONG" and curr >= t["tp"]) or \
                     (t["side"] == "SHORT" and curr <= t["tp"])
            if hit_tp:
                self.wins_today += 1
                _tp_ts[sym] = time.time()
                _last_ts.pop(sym, None)
                self._log_exit(t, curr, f"TP(#{self.wins_today})")
                signal_logger.info(
                    f"[WIN #{self.wins_today}] {sym} | +${t['pnl_usd']:.2f} | REPEAT READY in {TP_REPEAT_DELAY}s"
                )
                to_remove.append(t)
        
        for tr in to_remove:
            if tr in self.active_trades: self.active_trades.remove(tr)

    def _log_exit(self, t, price, reason):
        self.current_balance += t["pnl_usd"]
        self.history_count += 1
        with open(self.report_file, "a", encoding="utf-8") as f:
            f.write(f"| {datetime.now().strftime('%H:%M')} | {t['symbol']} | {t['side']} | {t['lev']}x | {t['entry']:.4f} | {price:.4f} | {t['pnl_pct']:+.2f}% | ${t['pnl_usd']:+.2f} | ${self.current_balance:.2f} ({reason}) |\n")
        log.info(f"  [EXIT] {t['symbol']} {reason}! PnL: ${t['pnl_usd']:+.2f} | New Balance: ${self.current_balance:.2f}")

    def print_dashboard(self, potentials: List[Dict] = None):
        now = datetime.now().strftime('%H:%M:%S')
        sep  = "=" * 82
        dash = "-" * 82
        log.info(sep)
        log.info(f"  🎯 SCALP & REPEAT | Balance: ${self.current_balance:.2f} | Target: ${self.target_balance:.2f}")
        log.info(f"  Gain: {((self.current_balance/self.initial_balance)-1)*100:+.2f}% | "
                 f"Wins: ✅{self.wins_today} | Losses: ❌{self.losses_today} | "
                 f"({now})")
        if potentials:
            log.info(dash)
            for p in potentials:
                log.info(f"  📡 SCOUT: {p['sym']}:{p['score']} {p['side']} | NEED: {p['missing']}")
        log.info(dash)
        if self.active_trades:
            log.info(f"  {'SYMBOL':<10} | {'SIDE':<5} | {'ENTRY':>9} | {'NOW':>9} | {'ROE%':>7} | {'PnL$':>8} | STATUS")
            log.info(dash)
            for t in self.active_trades:
                status = "🔒LOCKED" if t["at_lock_profit"] else ("🛡BE" if t["at_breakeven"] else "🔄OPEN")
                log.info(f"  {t['symbol']:<10} | {t['side']:<5} | {t['entry']:>9.4f} | "
                         f"{t.get('price_now',0):>9.4f} | {t['pnl_pct']:>7.1f}% | "
                         f"${t['pnl_usd']:>7.2f} | {status}")
            log.info(dash)
        else:
            log.info(f"  🔍 Hunting next $0.50 bag... (Score >= {MIN_SCORE}/6) [TF: 5m]")
            log.info(dash)

manager = AIFundManager(INITIAL_BALANCE, TARGET_BALANCE)

# =============================================================================
# LEVERAGE DATA
# =============================================================================

LEVERAGE_MAP = {}

def fetch_nado_leverages():
    global LEVERAGE_MAP
    try:
        import asyncio
        from client import NadoRestClient
        
        async def _fetch():
            rest = NadoRestClient()
            try:
                res = await rest.get_all_products()
                perps = res.get("perp_products", [])
                sym_res = await rest.query({"type": "symbols"})
                s_map = {v.get("product_id"): v.get("symbol") for k,v in sym_res.get("symbols", {}).items()}
                
                mapping = {}
                for p in perps:
                    pid = p.get('product_id')
                    raw_sym = s_map.get(pid, f"PID_{pid}")
                    sym = raw_sym.split("-")[0] # BTC instead of BTC-PERP
                    lw = float(p.get("risk", {}).get("long_weight_initial_x18", 0)) / 1e18
                    max_lev = round(1 / (1 - lw)) if (lw > 0 and lw < 1) else 1
                    mapping[sym] = max_lev
                return mapping
            finally:
                await rest.close()
        
        LEVERAGE_MAP = asyncio.run(_fetch())
        log.info(f"  [INIT] Loaded dynamic leverage for {len(LEVERAGE_MAP)} symbols.")
    except Exception as e:
        log.error(f"  [INIT] Failed to fetch leverage data: {e}. Using defaults.")
        LEVERAGE_MAP = {"BTC": 50, "ETH": 50, "SOL": 40}

# Initialize at startup
fetch_nado_leverages()

# =============================================================================
# STRATEGY & SCANNER
# =============================================================================

def score_signal(df1m: pd.DataFrame, price: float, base: str):
    """RAW BLIND SCALP — No detailed checks, just raw 1-minute momentum!"""
    if len(df1m) < 25: return None
    
    # Just look at 1-min raw momentum
    df1m["ema9"]  = ta.ema(df1m["Close"], length=9)
    df1m["ema21"] = ta.ema(df1m["Close"], length=21)
    df1m["atr"]   = ta.atr(df1m["High"], df1m["Low"], df1m["Close"], length=14)
    
    r = df1m.iloc[-1]
    
    # If EMA9 is above EMA21 -> LONG instantly. If below -> SHORT instantly.
    if r["ema9"] > r["ema21"]:
        side = "LONG"
    else:
        side = "SHORT"

    # Get Leverage
    lev = LEVERAGE_MAP.get(base, 10)
    atr = r["atr"] if pd.notna(r["atr"]) else (price * 0.002)

    # Wide enough SL so we don't get stopped out by noise, TP is hardcoded to $0.50 anyway
    if side == "LONG":
        sl = price - (1.5 * atr)
        tp = price + (4.0 * atr)
    else:
        sl = price + (1.5 * atr)
        tp = price - (4.0 * atr)

    return {"side": side, "entry": price, "sl": sl, "tp": tp, "lev": lev, "score": 6, "adx": 0}


def main():
    global _last_ts
    log.info("=" * 60)
    log.info(f"  🚀 Nado Managed Fund v5.0 'High-Confidence' | Target: ${TARGET_BALANCE:.2f}")
    log.info("=" * 60)
    yf_map = {}
    
    while manager.current_balance < TARGET_BALANCE:
        if not yf_map:
            try:
                r = requests.get(f"{GATEWAY}/query", params={"type": "symbols"}, timeout=10).json()
                for n, i in r.get("data", {}).get("symbols", {}).items():
                    if i.get("type") == "perp":
                        base = n.replace("-PERP", "").strip().upper()
                        if base not in SKIP_SYMBOLS:
                            yf_map[base] = f"{base}-USD"
            except Exception as e:
                log.error(f"  [API] Failed to fetch symbols from gateway: {e}")
                time.sleep(5)
                continue
        
        # Prioritize high-lev symbols first in scan order
        ordered = sorted(yf_map.keys(), key=lambda x: (0 if x in HIGH_LEV_SYMBOLS else 1, x))

        tickers = list(yf_map.values())
        if not tickers: 
            time.sleep(5)
            continue
            
        try:
            log.info(f"  [SCAN] Syncing {len(tickers)} symbols...")
            data = yf.download(tickers, period="2d", interval="1m", group_by="ticker", auto_adjust=True, progress=False, threads=True)
            prices = {}
            potentials = []
            
            for base in ordered:
                ticker = yf_map[base]
                df = data.get(ticker) if len(yf_map) > 1 else data
                if df is None or df.empty: continue
                price = float(df.iloc[-1]["Close"])
                if np.isnan(price): continue
                prices[base] = price
                
                # Active management loop (Frequent)
                manager.update(prices)
                
                # SL cooldown (20 min after loss)
                if time.time() - _sl_ts.get(base, 0) < SL_COOLDOWN: continue
                # TP repeat (30s after win)
                if time.time() - _tp_ts.get(base, 0) < TP_REPEAT_DELAY: continue
                
                sig = score_signal(df, price, base)
                if sig:
                    if not sig.get("potential"):
                        _last_ts[base] = time.time()
                        manager.add_trade(base, sig["side"], sig["entry"], sig["sl"], sig["tp"], sig["lev"], sig["score"])
                        signal_logger.info(
                            f"[ENTRY] {base} {sig['side']} | Price: {sig['entry']:.4f} | "
                            f"Lev: {sig['lev']}x | Score: {sig['score']} | Target: +${SCALP_TARGET}"
                        )
                    else:
                        if sig["score"] >= 4:
                            potentials.append({"sym": base, "score": sig["score"], "side": sig["side"], "missing": ", ".join(sig["missing"][:2])})
            
            # Sort potentials by score
            potentials.sort(key=lambda x: x["score"], reverse=True)
            manager.print_dashboard(potentials[:3])
            
        except Exception:
            import traceback
            log.error(f"Loop error:\n{traceback.format_exc()}")
        
        time.sleep(MONITOR_INTERVAL)

    log.info("=" * 60)
    log.info(f"🎉 GOAL REACHED! Final Balance: ${manager.current_balance:.2f}")
    log.info("=" * 60)

if __name__ == "__main__":
    main()
