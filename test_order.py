# -*- coding: utf-8 -*-
"""
Auto order placer — detects LONG/SHORT from RSI+EMA, rounds price correctly,
places order on Nado mainnet. No input needed.
Run: python test_order.py
"""
import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8")

from nado_api import (
    NadoRestClient, NadoSigner,
    PRIVATE_KEY, WALLET_ADDRESS, SUBACCOUNT,
    GATEWAY, CHAIN_ID, build_sender_hex, load_config,
    fetch_candles_yfinance, gen_order_verifying_contract,
    EIP712_DOMAIN_TEMPLATE, ORDER_TYPES, generate_nonce,
    build_sender, to_x18,
)
from eth_account.messages import encode_typed_data
from strategy import calc_rsi, calc_ema
import time

# ── Config ────────────────────────────────────────────────────────────────────
# Nado mainnet product IDs (verified by oracle price scan)
PRODUCT_IDS = {
    "BTC-PERP":  2,
    "ETH-PERP":  4,
    "SOL-PERP":  8,
    "XRP-PERP":  10,
    "BNB-PERP":  14,
}

TARGET_SYMBOL  = "ETH-PERP"
LEVERAGE       = 10
MARGIN_USD     = 1.0    # $1 margin -> $10 notional at 10x (min notional ~$100? Use enough)
# NOTE: Nado min notional for ETH-PERP = $100 (abs_amount_x18 * price_x18 >= 1e20)
# So notional >= $100 / 1e0 ... actually: amount*price >= min_size/1e18 = 100 USDT
# => MARGIN_USD * LEVERAGE >= 100 -> MARGIN_USD >= 10
MARGIN_USD     = 10.0   # $10 margin -> $100 notional at 10x (meets min notional)


def round_to_tick(value_x18: int, tick_x18: int) -> int:
    """Round x18 integer DOWN to nearest tick — avoids float precision issues."""
    return (value_x18 // tick_x18) * tick_x18


async def main():
    rest = NadoRestClient()

    # ── 1. Contracts & signer ─────────────────────────────────────────────────
    contracts     = await rest.query({"type": "contracts"})
    endpoint_addr = contracts.get("endpoint_addr", "0x0000000000000000000000000000000000000001")
    signer        = NadoSigner(PRIVATE_KEY, endpoint_addr, CHAIN_ID)

    cfg = load_config()
    print(f"GATEWAY  : {GATEWAY}")
    print(f"NETWORK  : {'MAINNET' if not cfg['network']['use_testnet'] else 'TESTNET'}")
    print(f"WALLET   : {WALLET_ADDRESS}")

    # ── 2. Balance ────────────────────────────────────────────────────────────
    sender  = build_sender_hex(WALLET_ADDRESS, SUBACCOUNT)
    balance = await rest.get_account_equity(sender)
    print(f"Balance  : ${balance:.4f} USDT\n")

    if balance < 1.0:
        print("ERROR: Balance too low (<$1). Aborting.")
        await rest.close(); return

    # ── 3. Product data (price, ticks) ───────────────────────────────────────
    products = await rest.query({"type": "all_products"})
    pid      = PRODUCT_IDS.get(TARGET_SYMBOL)

    oracle_x18        = 0
    price_tick_x18    = 1_000_000_000_000_000_000  # default $1 tick
    size_tick_x18     = 50_000_000_000_000          # default
    min_size_x18      = 0

    for p in products.get("perp_products", []):
        if p.get("product_id") == pid:
            oracle_x18     = int(p.get("oracle_price_x18", 0))
            book            = p.get("book_info", {})
            price_tick_x18  = int(book.get("price_increment_x18", price_tick_x18))
            size_tick_x18   = int(book.get("size_increment", size_tick_x18))
            min_size_x18    = int(book.get("min_size", 0))
            break

    if oracle_x18 <= 0:
        print(f"ERROR: {TARGET_SYMBOL} (id={pid}) price=0"); await rest.close(); return

    # Round DOWN to nearest price tick (integer arithmetic — no float error)
    oracle_x18_rounded = round_to_tick(oracle_x18, price_tick_x18)
    mid = oracle_x18_rounded / 1e18

    price_tick = price_tick_x18 / 1e18
    size_tick  = size_tick_x18  / 1e18
    min_size   = min_size_x18   / 1e18

    print(f"Product    : {TARGET_SYMBOL}  id={pid}")
    print(f"Price tick : ${price_tick}  Size tick: {size_tick}")
    print(f"Oracle     : ${oracle_x18/1e18:.4f}  -> rounded: ${mid:.4f}")

    # ── 4. Direction from RSI + EMA ───────────────────────────────────────────
    candle_df = fetch_candles_yfinance([TARGET_SYMBOL], period="5d", lookback=80).get(TARGET_SYMBOL)
    direction_label = "LONG"

    if candle_df is not None and len(candle_df) >= 25:
        rsi      = calc_rsi(candle_df["Close"], 14)
        ema_fast = calc_ema(candle_df["Close"], 9)
        ema_slow = calc_ema(candle_df["Close"], 21)
        trend    = ("bullish" if ema_fast > ema_slow * 1.001
                    else "bearish" if ema_fast < ema_slow * 0.999
                    else "neutral")

        print(f"\nRSI(14)   : {rsi:.1f}")
        print(f"EMA 9/21  : {ema_fast:.2f} / {ema_slow:.2f}  [{trend.upper()}]")

        if rsi > 60 or trend == "bearish":
            direction_label = "SHORT"
        else:
            direction_label = "LONG"
    else:
        print("Indicators: not enough candle data -- defaulting LONG")

    direction = 1.0 if direction_label == "LONG" else -1.0
    print(f"Direction  : {direction_label}\n")

    # ── 5. Size calculation (rounded to size_tick) ───────────────────────────
    notional     = MARGIN_USD * LEVERAGE        # $5 notional
    amount_raw   = (notional / mid) * direction

    # Round amount UP to nearest size_tick (ceiling) so notional meets min_size
    amount_x18_raw     = int(abs(amount_raw) * 1e18)
    if size_tick_x18 > 0:
        amount_x18_rounded = ((amount_x18_raw + size_tick_x18 - 1) // size_tick_x18) * size_tick_x18
    else:
        amount_x18_rounded = amount_x18_raw
    amount_abs         = amount_x18_rounded / 1e18
    amount             = amount_abs * direction

    print(f"Order      : {direction_label} {amount_abs:.6f} {TARGET_SYMBOL.replace('-PERP','')} @ ${mid:.4f}")
    notional_usd = amount_abs * mid
    print(f"Notional   : ${notional_usd:.2f}  Margin ~${notional_usd / LEVERAGE:.2f}  Lev {LEVERAGE}x")

    # Check min notional: abs(amount_x18) * price_x18 >= min_size_raw
    # min_size_raw = 100000000000000000000 (from product data)
    min_notional_check = (amount_x18_rounded * oracle_x18_rounded) // (10**18)
    min_size_raw = min_size_x18  # raw value from API is the threshold
    print(f"  notional check: {min_notional_check} >= {min_size_raw} ? {min_notional_check >= min_size_raw}")

    if min_notional_check < min_size_raw:
        needed_amount = min_size_raw / oracle_x18_rounded  # in x18 base units
        min_usd = (needed_amount / 1e18) * mid
        print(f"  Min notional not met! Need at least ${min_usd:.2f} notional")
        print(f"  Increase MARGIN_USD to >= ${min_usd/LEVERAGE:.2f}")
        await rest.close(); return

    if amount_abs <= 0:
        print(f"ERROR: Amount is 0. Aborting.")
        await rest.close(); return

    # Sanity: must be at least 1 size_tick
    if amount_x18_rounded < size_tick_x18:
        print(f"ERROR: Amount {amount_abs:.8f} < size_tick {size_tick:.8f}. Aborting.")
        await rest.close(); return

    # ── 6. Build & sign payload manually (with clean x18 ints) ───────────────
    print("\nSigning order...")
    expiration   = int(time.time()) + 300
    nonce        = generate_nonce()
    appendix     = 1   # standard market order

    sender_bytes = build_sender(WALLET_ADDRESS, SUBACCOUNT)
    sender_hex   = "0x" + sender_bytes.hex()

    # Verify contract = address(productId) per Nado docs
    verify_contract = gen_order_verifying_contract(pid)

    domain = {
        **EIP712_DOMAIN_TEMPLATE,
        "chainId": CHAIN_ID,
        "verifyingContract": verify_contract,
    }

    message = {
        "sender":     sender_bytes,
        "priceX18":   oracle_x18_rounded,          # already rounded int
        "amount":     int(amount * 1e18),           # signed int
        "expiration": expiration,
        "nonce":      nonce,
        "appendix":   appendix,
    }

    structured = {
        "types":       ORDER_TYPES,
        "primaryType": "Order",
        "domain":      domain,
        "message":     message,
    }

    from eth_account import Account
    account   = Account.from_key(PRIVATE_KEY)
    signable  = encode_typed_data(full_message=structured)
    signed    = account.sign_message(signable)
    signature = "0x" + signed.signature.hex()

    payload = {
        "place_order": {
            "product_id": pid,
            "order": {
                "sender":     sender_hex,
                "priceX18":   str(oracle_x18_rounded),
                "amount":     str(int(amount * 1e18)),
                "expiration": str(expiration),
                "nonce":      str(nonce),
                "appendix":   str(appendix),
            },
            "signature": signature,
            "id": nonce % 100000,
        }
    }

    print(f"  verifyContract : {verify_contract}")
    print(f"  priceX18       : {oracle_x18_rounded}")
    print(f"  amountX18      : {int(amount * 1e18)}")

    # ── 7. Place order ────────────────────────────────────────────────────────
    print("\nPlacing to mainnet...")
    try:
        result = await rest.place_order(payload)
        print(f"\n[OK] ORDER PLACED!")
        print(f"     {direction_label} {amount_abs:.6f} @ ${mid:.4f}")
        print(f"     result: {result}")
    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback; traceback.print_exc()

    await rest.close()


if __name__ == "__main__":
    asyncio.run(main())
