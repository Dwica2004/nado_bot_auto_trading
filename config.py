# ABOUTME: Configuration for Nado scalping bot.
# ABOUTME: All API endpoints, strategy params, and risk settings.

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

# ─── Nado API Endpoints ────────────────────────────────────────────────────────
NADO_TESTNET  = "https://gateway.test.nado.xyz"
NADO_MAINNET  = "https://gateway.prod.nado.xyz/v1"
NADO_ARCHIVE  = "https://archive.test.nado.xyz/v2"

WS_TESTNET    = "wss://gateway.test.nado.xyz/ws"
WS_MAINNET    = "wss://gateway.prod.nado.xyz/v1/ws"

SUB_TESTNET   = "wss://subscriptions.test.nado.xyz/ws"
SUB_MAINNET   = "wss://subscriptions.nado.xyz/ws"

# ─── EIP712 Domain (Nado Testnet) ─────────────────────────────────────────────
CHAIN_ID_TESTNET = 763373
CHAIN_ID_MAINNET = 57073

EIP712_DOMAIN_TEMPLATE = {
    "name": "Nado",
    "version": "0.0.1",
    "chainId": None,          # filled at runtime
    "verifyingContract": None # filled from /query?type=contracts
}

ORDER_TYPES = {
    "EIP712Domain": [
        {"name": "name",              "type": "string"},
        {"name": "version",           "type": "string"},
        {"name": "chainId",           "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "sender",    "type": "bytes32"},
        {"name": "priceX18",  "type": "int128"},
        {"name": "amount",    "type": "int128"},
        {"name": "expiration","type": "uint64"},
        {"name": "nonce",     "type": "uint64"},
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

# ─── Wallet Config (from .env) ────────────────────────────────────────────────
PRIVATE_KEY     = os.getenv("PRIVATE_KEY", "")          # 0x...
WALLET_ADDRESS  = os.getenv("WALLET_ADDRESS", "")        # 0x...
SUBACCOUNT_NAME = os.getenv("SUBACCOUNT_NAME", "").strip() or "default"

# ─── Trading Settings ─────────────────────────────────────────────────────────
USE_TESTNET     = os.getenv("USE_TESTNET", "true").lower() == "true"

# Token pairs to explicitly ignore (stocks, forex, and unlisted meme-coins)
BLACKLIST_TOKENS = [
    "SUI", "MSFT", "AMZN", "WLFI", "XAG", "AAPL", "TAO", "NVDA", "ZRO", 
    "ASTER", "TSLA", "PENGU", "HYPE", "USELESS", "QQQ", "GOOGL", "SPY", 
    "LIT", "UNI", "KPEPE", "WTI", "USDJPY", "EURUSD", "PUMP", "KBONK", "GBPUSD"
]

# ─── Strategy Parameters (BB + EMA + RSI + ATR + OTL + FIBO) ──────────────────
@dataclass
class StrategyConfig:
    # Candle timeframe for signal generation
    candle_interval:  str   = "5m"      # 5-minute candles
    candle_lookback:  int   = 100       # number of candles to load

    # EMA Trend Filter
    ema_fast:         int   = 9
    ema_slow:         int   = 21

    # Bollinger Bands (mean reversion entry)
    bb_period:        int   = 20
    bb_std:           float = 2.0
    bb_entry_buffer:  float = 0.005     # 0.5% inside band (wider = more entries)

    # RSI Filter — WIDENED for aggressive scalping
    rsi_period:       int   = 14
    rsi_long_max:     float = 50.0      # Long when RSI < 50 (wider window)
    rsi_short_min:    float = 50.0      # Short when RSI > 50 (wider window)

    # ATR-based Stop Loss & Take Profit — tight for scalping
    atr_period:       int   = 14
    atr_sl_mult:      float = 1.2       # SL = 1.2 × ATR (protects balance)
    atr_tp_mult:      float = 1.0       # TP = 1.0 × ATR (fast exit, cepat ambil profit)

    # Signal cooldown (seconds) — low for aggressive
    signal_cooldown:  int   = 30

# ─── Risk Management ──────────────────────────────────────────────────────────
@dataclass
class RiskConfig:
    # % saldo untuk max notional per trade (30% saldo → ~1.75 USDT dari 5.82)
    max_position_pct:   float = 0.30

    # % saldo sebagai daily loss limit (15% saldo = ~0.87 USDT → aman)
    daily_loss_pct:     float = 0.15

    # % saldo yang di-risk per trade (10% = ~0.58 USDT per trade)
    risk_per_trade_pct: float = 0.10

    # Hanya 1 posisi terbuka sekaligus
    max_open_trades:    int   = 1

    # Floor minimum agar tidak trade dengan saldo kritis
    min_balance_usdt:   float = 2.0

    def max_position_usdt(self, balance: float) -> float:
        """Max notional per trade = max_position_pct × saldo."""
        return balance * self.max_position_pct

    def daily_loss_limit(self, balance: float) -> float:
        """Stop trading jika rugi melebihi daily_loss_pct × saldo."""
        return balance * self.daily_loss_pct

    def risk_usdt(self, balance: float) -> float:
        """USDT yang di-risk per trade = risk_per_trade_pct × saldo."""
        return balance * self.risk_per_trade_pct

    def summary(self, balance: float) -> dict:
        return {
            "balance_usdt":       round(balance, 2),
            "max_position_usdt":  round(self.max_position_usdt(balance), 2),
            "daily_loss_limit":   round(self.daily_loss_limit(balance), 2),
            "risk_per_trade":     round(self.risk_usdt(balance), 2),
            "risk_per_trade_pct": f"{self.risk_per_trade_pct:.1%}",
        }

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE  = os.getenv("LOG_FILE", "nado_bot.log")

STRATEGY = StrategyConfig()
RISK     = RiskConfig()
