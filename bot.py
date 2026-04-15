# ABOUTME: Main trading bot — orchestrates strategy, risk, signing, and execution.
# ABOUTME: Runs async event loop: fetch candles → signal → size → sign → execute over all valid perps.

import asyncio
import json
import logging
import sys
import time
from typing import Optional

from config import (
    USE_TESTNET, PRIVATE_KEY, WALLET_ADDRESS, SUBACCOUNT_NAME,
    STRATEGY, RISK, LOG_LEVEL, LOG_FILE, BLACKLIST_TOKENS
)
from client   import NadoRestClient, NadoPriceFeed
from signing  import NadoSigner, build_sender_hex, from_x18
from strategy import ScalpingStrategy, Signal, TradeSignal, bulk_fetch_candles
from risk     import RiskManager

# ─── Logging Setup ────────────────────────────────────────────────────────────

def setup_logging():
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        pass
        
    fmt = "%(asctime)s [%(levelname)s] %(name)s | %(message)s"
    handlers = [logging.StreamHandler(sys.stdout)]
    if LOG_FILE:
        handlers.append(logging.FileHandler(LOG_FILE, encoding='utf-8'))
    logging.basicConfig(level=getattr(logging, LOG_LEVEL, "INFO"), format=fmt, handlers=handlers, force=True)

logger = logging.getLogger("nado_bot")

# ─── Nado Trading Bot ─────────────────────────────────────────────────────────

class NadoScalpingBot:
    """
    Full-cycle async scalping bot for ALL pairs:
      1. Live price → NadoPriceFeed (WebSocket bulk)
      2. Signal → ScalpingStrategy + bulk fetching yfinance
      3. Risk  → RiskManager (sizing, daily limits, SL/TP monitoring)
      4. Order → NadoSigner + NadoRestClient
    """

    def __init__(self):
        self.rest     = NadoRestClient()
        self.strategy = ScalpingStrategy(STRATEGY)
        self.risk     = RiskManager(RISK)
        self.signer:  Optional[NadoSigner] = None
        self.feed:    Optional[NadoPriceFeed] = None

        self._current_prices: dict[int, float] = {}
        self._product_map: dict[int, str] = {}    # id -> Nado Symbol (e.g. BTC-PERP)
        self._yfinance_map: dict[int, str] = {}   # id -> yfinance Symbol (e.g. BTC-USD)
        
        self._running              = False
        self._sender_hex           = build_sender_hex(WALLET_ADDRESS, SUBACCOUNT_NAME) if WALLET_ADDRESS else ""

    # ─── Startup ──────────────────────────────────────────────────────────────

    async def start(self):
        """Initialize connections, signer, and start the main loop."""
        setup_logging()
        logger.info("=" * 60)
        logger.info("  Nado Scalping Bot  |  All Pairs Analysis")
        logger.info(f"  Testnet: {USE_TESTNET}")
        logger.info("=" * 60)

        if not PRIVATE_KEY or not WALLET_ADDRESS:
            logger.error("PRIVATE_KEY and WALLET_ADDRESS must be set in .env")
            return

        logger.info("Fetching Nado contract addresses …")
        contracts    = await self.rest.get_contracts()
        endpoint_addr = contracts.get("endpoint_addr", "")
        chain_id      = contracts.get("chain_id", "")
        logger.info(f"Contracts -> endpoint={endpoint_addr} | chain_id={chain_id}")

        self.signer = NadoSigner(PRIVATE_KEY, endpoint_addr, testnet=USE_TESTNET)

        logger.info("Fetching all products…")
        products = await self.rest.get_all_products()
        perps = products.get('perp_products', [])
        logger.info(f"Products available: {len(products.get('spot_products', []))} spot, {len(perps)} perps")

        logger.info("Fetching product symbols…")
        symbols_res = await self.rest.query({"type": "symbols"})
        
        # Build symbol map
        sym_map = {}
        for key, val in symbols_res.get("symbols", {}).items():
            sym_map[val.get("product_id")] = val.get("symbol")

        for p in perps:
            pid = p.get("product_id")
            nado_symbol = sym_map.get(pid, f"UNKNOWN-{pid}")
            base_token = nado_symbol.replace("-PERP", "")
            if base_token in BLACKLIST_TOKENS:
                continue
                
            if nado_symbol.endswith("-PERP"):
                yf_symbol = nado_symbol.replace("-PERP", "-USD")
                self._product_map[pid] = nado_symbol
                self._yfinance_map[pid] = yf_symbol

        logger.info(f"Tracking {len(self._product_map)} perpetual markets internally.")

        logger.info("Fetching initial balance …")
        initial_balance = await self.rest.get_account_equity(self._sender_hex)
        self.risk.update_balance_snapshot(initial_balance)
        
        self.feed = NadoPriceFeed(
            product_ids    = list(self._product_map.keys()),
            price_callback = self._on_price_update,
        )

        self._running = True
        await asyncio.gather(
            self.feed.run(),
            self._main_loop(),
        )

    async def stop(self):
        self._running = False
        if self.feed:
            await self.feed.stop()
        await self.rest.close()
        logger.info("Bot stopped.")
        
        final_balance = None
        if self.rest and self._sender_hex:
            final_balance = await self.rest.get_account_equity(self._sender_hex)
            
        logger.info(f"Session summary:\n{json.dumps(self.risk.summary(final_balance or 0.0), indent=2)}")

    # ─── Price Callback ───────────────────────────────────────────────────────

    def _on_price_update(self, product_id: int, bid: float, ask: float, mid: float):
        self._current_prices[product_id] = mid

    # ─── Main Loop ────────────────────────────────────────────────────────────

    async def _main_loop(self):
        # Wait for prices to populate
        logger.info("Loading websocket prices...")
        await asyncio.sleep(3)

        while self._running:
            try:
                loop_start = time.time()
                
                logger.info(f"⏳ Starting 1-minute analysis cycle for {len(self._yfinance_map)} pairs...")
                
                # ── Analysis step (Every ~60 seconds) ──────────────────────
                balance = await self.rest.get_account_equity(self._sender_hex)
                can_trade, reason = self.risk.can_trade(balance)
                
                if not can_trade:
                    logger.info(f"Skipping signal check: {reason}")
                else:
                    await self._evaluate_all_pairs(balance)

                # Monior positions & Wait until the 60 seconds are up since loop_start
                # While we spin waiting, we monitor active trade if any.
                while self._running and (time.time() - loop_start < 60):
                    if self.risk.open_position is not None:
                        pid = self.risk.open_position.product_id
                        price = self._current_prices.get(pid, 0.0)
                        if price > 0:
                            await self._monitor_position(price)
                    await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                await asyncio.sleep(5)

    # ─── Multi-Pair Evaluation ────────────────────────────────────────────────

    async def _evaluate_all_pairs(self, balance: float):
        symbols_to_fetch = list(self._yfinance_map.values())
        
        df_map = bulk_fetch_candles(symbols_to_fetch, STRATEGY.candle_lookback)
        
        failed_pids = []
        signals_found = 0
        pairs_with_price = 0
        pairs_no_price = 0
        
        for pid, nado_sym in self._product_map.items():
            yf_sym = self._yfinance_map[pid]
            
            df_dict = df_map.get(yf_sym)
            if not df_dict or "1m" not in df_dict or df_dict["1m"].empty:
                failed_pids.append(pid)
                continue
                
            # Fallback to the latest closed candle close price
            current_price = self._current_prices.get(pid, 0.0)
            if current_price == 0.0:
                current_price = float(df_dict["1m"].iloc[-1]["Close"])
                
            pairs_with_price += 1
                
            signal = self.strategy.generate(
                current_price=current_price,
                product_id=pid,
                df_map=df_dict,
                symbol=yf_sym
            )
            
            # AGGRESSIVE: Take the FIRST valid signal immediately
            if signal.signal != Signal.NONE:
                signals_found += 1
                logger.info(f"Signal found: {nado_sym} {signal.signal.value} | {signal.reason}")
                amount = self.risk.calc_position_size(balance, signal.entry, signal.sl)
                if amount > 0:
                    await self._execute_entry(pid, signal, amount)
                    return  # Done — we entered a trade
                else:
                    logger.warning(f"Position size is 0 for {nado_sym} — trying next pair")

        if failed_pids:
            for pid in failed_pids:
                self._product_map.pop(pid, None)
                self._yfinance_map.pop(pid, None)
            logger.info(f"Dropped {len(failed_pids)} unavailable pairs from active scan.")
        
        logger.info(f"Scan complete | {pairs_with_price} priced | {pairs_no_price} no price | {signals_found} signals | no entry")

    # ─── Entry Execution ──────────────────────────────────────────────────────

    async def _execute_entry(self, product_id: int, signal: TradeSignal, amount: float):
        direction = 1.0 if signal.signal == Signal.LONG else -1.0
        signed_amount = amount * direction

        sym = self._product_map[product_id]
        logger.info(
            f"{'🟢 BUYING' if direction > 0 else '🔴 SELLING'} {sym} "
            f"size={amount:.6f} @ {signal.entry:.4f} | SL={signal.sl:.4f} | TP={signal.tp:.4f} | "
            f"RR={signal.rr} | RSI={signal.rsi}"
        )

        try:
            payload = self.signer.sign_order(
                product_id = product_id,
                price      = signal.entry,
                amount     = signed_amount,
                subaccount = SUBACCOUNT_NAME,
            )

            result   = await self.rest.place_order(payload)
            order_id = result.get("digest") or result.get("order_id") or "unknown"

            self.risk.open_trade(product_id, signal, amount, order_id=str(order_id))

        except Exception as e:
            logger.error(f"Entry execution failed for {sym}: {e}", exc_info=True)

    # ─── SL/TP Monitoring ─────────────────────────────────────────────────────

    async def _monitor_position(self, price: float):
        pos = self.risk.open_position
        if pos is None:
            return

        if pos.should_take_profit(price):
            await self._execute_exit(price, "TAKE PROFIT")
        elif pos.should_stop_loss(price):
            await self._execute_exit(price, "STOP LOSS")
        else:
            if int(time.time()) % 30 == 0:
                unrealized = pos.pnl(price)
                sym = self._product_map.get(pos.product_id, "UNK")
                logger.debug(
                    f"Pos alive | {sym} {pos.signal.value} | "
                    f"entry={pos.entry_price:.4f} | now={price:.4f} | "
                    f"unrealized={unrealized:+.4f} USDT | "
                    f"SL={pos.sl_price:.4f} | TP={pos.tp_price:.4f}"
                )

    async def _execute_exit(self, price: float, reason: str):
        pos = self.risk.open_position
        if pos is None:
            return

        close_direction = -1.0 if pos.is_long else 1.0
        signed_amount   = pos.amount * close_direction
        
        sym = self._product_map.get(pos.product_id, "UNK")
        logger.info(f"Closing position {sym} | reason={reason} | price={price:.4f}")

        try:
            payload = self.signer.sign_order(
                product_id = pos.product_id,
                price      = price,
                amount     = signed_amount,
                subaccount = SUBACCOUNT_NAME,
            )

            await self.rest.place_order(payload)
            pnl = self.risk.close_trade(price, reason)
            logger.info(f"Exit done | PnL={pnl:+.4f} USDT")

        except Exception as e:
            logger.error(f"Exit execution failed: {e}", exc_info=True)
            self.risk.close_trade(price, f"FORCED ({reason})")

if __name__ == "__main__":
    pass
