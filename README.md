# 🤖 Nado Auto Trader

Fully automated crypto trading bot for **Nado DEX** (Ink L2) that executes trades based on news sentiment, price breakout confirmation, and volume spikes.

---

## ⚡ Features

| Feature | Description |
|---------|-------------|
| **News Sentiment** | Scrapes CoinDesk, CoinTelegraph, CryptoCompare + Twitter/Nitter for real-time crypto sentiment |
| **Triple Confirmation** | Trades ONLY when sentiment + breakout + volume spike ALL align |
| **EIP-712 Signing** | Full wallet integration with Nado's EIP-712 order signing (mainnet + testnet) |
| **Risk Management** | 2% risk per trade, stop loss, take profit, 1 active trade max |
| **Backtesting** | Test strategy on historical data before going live |
| **CLI Dashboard** | Real-time terminal output with position tracking and PnL |
| **Trade Logging** | All trades logged with timestamp, signal, entry, exit, PnL |

---

## 📁 Project Structure

```
Nado_auto_trading/
├── main.py          # Entry point — trading loop + CLI dashboard
├── nado_api.py      # Nado REST client + EIP-712 signer + candle cache
├── news.py          # RSS + Twitter scraper + sentiment analysis
├── strategy.py      # Entry strategy (sentiment + breakout + volume)
├── backtest.py      # Backtesting engine
├── config.json      # All tunable parameters
├── requirements.txt # Python dependencies
├── .env.example     # Environment variable template
└── .env             # Your private keys (DO NOT COMMIT)
```

---

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your wallet details:

```env
PRIVATE_KEY=0xYOUR_PRIVATE_KEY
WALLET_ADDRESS=0xYOUR_WALLET_ADDRESS
```

### 3. Configure Strategy (Optional)

Edit `config.json` to tune:
- Trading pairs (`trading.product_symbols`)
- Risk parameters (`risk.risk_per_trade_pct`, `risk.stop_loss_pct`, etc.)
- News sources (`news.rss_feeds`)
- Loop interval (`trading.loop_interval_seconds`)

### 4. Run the Bot

```bash
python main.py
```

### 5. Run Backtesting

```bash
python backtest.py --symbol BTC-PERP --period 30d --interval 5m
```

---

## 🧠 Trading Strategy

The bot uses a **triple-confirmation** system. ALL conditions must be met:

### Long Entry
```
✅ News sentiment == "positive"
✅ Price breaks above resistance (highest of last 20 candles)
✅ Volume spike > 1.5x average volume
```

### Short Entry
```
✅ News sentiment == "negative"
✅ Price breaks below support (lowest of last 20 candles)
✅ Volume spike > 1.5x average volume
```

### Risk Management
- **Risk per trade**: 2% of total balance
- **Stop Loss**: 1.5% from entry
- **Take Profit**: 3% from entry
- **Max open trades**: 1
- **Cooldown**: 45 seconds after each trade

---

## 📰 News Sources

| Source | Type | Method |
|--------|------|--------|
| CoinTelegraph | RSS | feedparser |
| CoinDesk | RSS | feedparser |
| Decrypt | RSS | feedparser |
| Bitcoin Magazine | RSS | feedparser |
| Twitter/X | Social | Nitter scraping |
| CryptoCompare | API | REST (fallback) |

### Sentiment Keywords

**Positive**: bullish, breakout, listing, partnership, launch, rally, surge, adoption, approved, etf...

**Negative**: hack, exploit, dump, bearish, selloff, crash, scam, liquidation, ban, crackdown...

---

## ⚙️ Configuration

All settings in `config.json`:

```json
{
    "strategy": {
        "candle_count": 50,
        "breakout_lookback": 20,
        "volume_spike_multiplier": 1.5
    },
    "risk": {
        "risk_per_trade_pct": 0.02,
        "stop_loss_pct": 0.015,
        "take_profit_pct": 0.03,
        "max_open_trades": 1
    }
}
```

---

## 🔒 Safety Features

- **No overtrading**: Max 1 position + cooldown timer
- **Market unclear**: Skips when sentiment is neutral
- **Balance guard**: Won't trade below minimum balance
- **Retry logic**: Auto-retries failed API calls (3 attempts)
- **Graceful shutdown**: Ctrl+C stops cleanly with session summary

---

## ⚠️ Disclaimer

This bot is for **educational purposes**. Trading crypto involves significant risk. Never trade with funds you can't afford to lose. Always test on **testnet** first.

---

## 📊 CLI Dashboard Example

```
  ╔════════════════════════════════════════════════════════════════════════╗
  ║  🤖 NADO AUTO TRADER  │  TESTNET  │  14:30:22                       ║
  ╚════════════════════════════════════════════════════════════════════════╝

  💰 Balance: $542.18  │  Sentiment: 🟢 POSITIVE (+1.50)
  📊 Trades: 12W/3L (80%)  │  PnL: $+42.18
  ────────────────────────────────────────────────────────────────────────
  📌 ACTIVE: BTC-PERP LONG
     Entry: 83521.4200  │  Now: 83892.1000  │  🟢 PnL: $+2.4100
     SL: 82268.60  │  TP: 86026.06
  ────────────────────────────────────────────────────────────────────────
  📰 Recent News:
     🟢 Bitcoin breaks $84K as institutional demand surges
     🟢 Major partnership announced between Ethereum Foundation and...
     ⚪ Crypto market consolidates ahead of Fed meeting
  ────────────────────────────────────────────────────────────────────────
  📈 BTC-PERP: $83,892.10  │  ETH-PERP: $3,421.50  │  SOL-PERP: $178.33
```
