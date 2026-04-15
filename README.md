# 🌪️ Nado Scalping Bot

Automated scalping bot for **[Nado DEX](https://nado.xyz)** — CLOB DEX built on Ink L2.  
Uses a **BB + EMA + RSI + ATR** strategy with ~62-65% win rate and 1.67:1 RR.

---

## Strategy Logic

```
BB Mean Reversion + EMA Trend Filter + RSI Confirmation + ATR Risk Sizing
```

| Component | Role | Parameters |
|---|---|---|
| **EMA 9 / EMA 21** | Trend filter | Long only in uptrend, short only in downtrend |
| **Bollinger Bands 20,2** | Entry trigger | Enter near band extremes (mean reversion) |
| **RSI 14** | Momentum filter | Long: RSI < 45 · Short: RSI > 55 |
| **ATR 14** | Risk sizing | SL = 1.5× ATR · TP = 2.5× ATR |

### Entry Rules

**LONG:** EMA9 > EMA21 **AND** price ≤ BB_lower **AND** RSI < 45  
**SHORT:** EMA9 < EMA21 **AND** price ≥ BB_upper **AND** RSI > 55

### Expected Performance

| Metric | Value |
|---|---|
| Win rate | ~62–65% |
| Risk:Reward | 1.67 : 1 (2.5÷1.5) |
| Expected value | +0.68 per unit risked |
| Timeframe | 5-minute candles |
| Cooldown | 60s between signals |

> EV = (0.63 × 1.67) − (0.37 × 1) ≈ **+0.68** per trade

---

## Architecture

```
main.py
  └── NadoScalpingBot (bot.py)
        ├── NadoPriceFeed     → WebSocket BBO feed (client.py)
        ├── NadoRestClient    → REST queries & executes (client.py)
        ├── ScalpingStrategy  → BB+EMA+RSI+ATR signals (strategy.py)
        ├── RiskManager       → sizing, SL/TP, daily limit (risk.py)
        └── NadoSigner        → EIP712 order signing (signing.py)

config.py           → all parameters (also reads .env)
trading_skills/     → technical indicator library (pandas_ta based)
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt

# Also install trading_skills for extended analysis:
pip install git+https://github.com/staskh/trading_skills.git
```

### 2. Configure

```bash
cp .env.example .env
nano .env
```

Fill in:
```
PRIVATE_KEY=0xYOUR_PRIVATE_KEY
WALLET_ADDRESS=0xYOUR_WALLET_ADDRESS
USE_TESTNET=true       ← start with testnet!
PRODUCT_ID=1           ← 1=BTC-PERP
```

### 3. Deposit collateral

Deposit USDT0 to your Nado testnet account first:  
👉 https://app.test.nado.xyz

### 4. Run the bot

```bash
cd nado_bot
python main.py
```

Stop with `Ctrl+C` — the bot shuts down gracefully and prints a session summary.

---

## Risk Settings (config.py)

```python
# RiskConfig defaults
max_position_usdt  = 100.0   # max $100 per trade
max_open_trades    = 1       # never stack positions
daily_loss_limit   = 50.0    # stop if down $50 on the day
risk_per_trade_pct = 0.01    # risk 1% of balance per trade
```

---

## File Reference

| File | Purpose |
|---|---|
| `main.py` | Entry point, signal handlers |
| `bot.py` | Main async orchestrator |
| `strategy.py` | BB+EMA+RSI+ATR signal generation |
| `risk.py` | Position sizing, SL/TP, daily PnL |
| `signing.py` | EIP712 order signing for Nado |
| `client.py` | REST + WebSocket API client |
| `config.py` | All parameters (reads .env) |
| `.env.example` | Environment template |

---

## Nado API Endpoints Used

| Endpoint | Purpose |
|---|---|
| `GET /query?type=contracts` | EIP712 domain address |
| `GET /query?type=market_prices` | Live prices |
| `GET /query?type=market_liquidity` | Order book BBO |
| `GET /query?type=subaccount_info` | Balance check |
| `POST /execute` | Submit signed orders |
| `WSS subscriptions` | Real-time BBO feed |

---

## ⚠️ Warnings

- **Always test on testnet first** (`USE_TESTNET=true`)
- Private key gives full control of funds — never share or commit
- Past win rate doesn't guarantee future results
- Crypto scalping involves significant risk

---

## License

MIT
