# ABOUTME: Nado API client — REST queries/executes and WebSocket subscriptions.
# ABOUTME: Handles connection, ping/pong keepalive, and order lifecycle.

import asyncio
import json
import logging
import time
from typing import Callable, Optional

import aiohttp
import websockets

from config import (
    NADO_TESTNET, NADO_MAINNET, WS_TESTNET, WS_MAINNET,
    SUB_TESTNET, SUB_MAINNET, USE_TESTNET
)

logger = logging.getLogger(__name__)

GATEWAY  = NADO_TESTNET  if USE_TESTNET else NADO_MAINNET
WS_URL   = WS_TESTNET    if USE_TESTNET else WS_MAINNET
SUB_URL  = WS_URL  # BBO subscriptions go through gateway WS on mainnet

HEADERS  = {
    "Content-Type":   "application/json",
    "Accept-Encoding": "gzip, deflate, br",
}


# ─── REST Client ──────────────────────────────────────────────────────────────

class NadoRestClient:
    """Thin async REST wrapper for Nado gateway queries and executes."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=HEADERS)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Queries ────────────────────────────────────────────────────────────────

    async def query(self, params: dict) -> dict:
        """GET /query with url-encoded params."""
        session = await self._get_session()
        url     = f"{GATEWAY}/query"
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status} on {url}: {text[:100]}")
            try:
                data = await resp.json()
            except Exception as e:
                text = await resp.text()
                raise RuntimeError(f"Invalid JSON response on {url} (HTTP {resp.status}): {text[:100]}")
                
            if data.get("status") != "success":
                raise RuntimeError(f"Query failed: {data.get('error', data)}")
            return data.get("data", {})

    async def get_contracts(self) -> dict:
        return await self.query({"type": "contracts"})

    async def get_all_products(self) -> dict:
        return await self.query({"type": "all_products"})

    async def get_market_prices(self) -> dict:
        return await self.query({"type": "market_prices"})

    async def get_market_liquidity(self, product_id: int, depth: int = 5) -> dict:
        return await self.query({"type": "market_liquidity", "product_id": product_id, "depth": depth})

    async def get_subaccount_info(self, sender: str) -> dict:
        return await self.query({"type": "subaccount_info", "subaccount": sender})

    async def get_open_orders(self, sender: str, product_id: int) -> list:
        data = await self.query({"type": "subaccount_orders", "subaccount": sender, "product_id": product_id})
        return data.get("orders", [])

    async def get_fee_rates(self, sender: str) -> dict:
        return await self.query({"type": "fee_rates", "subaccount": sender})

    async def get_nonce(self, address: str) -> int:
        data = await self.query({"type": "nonces", "address": address})
        return data.get("order_nonce", 0)

    # ── Executes ───────────────────────────────────────────────────────────────

    async def execute(self, payload: dict) -> dict:
        """POST /execute with JSON payload."""
        session = await self._get_session()
        url     = f"{GATEWAY}/execute"
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"HTTP {resp.status} on {url}: {text[:100]}")
            try:
                data = await resp.json()
            except Exception as e:
                text = await resp.text()
                raise RuntimeError(f"Invalid JSON response on {url} (HTTP {resp.status}): {text[:100]}")
                
            if data.get("status") != "success":
                raise RuntimeError(f"Execute failed: {data.get('error', data)}")
            return data.get("data", {})

    async def place_order(self, payload: dict) -> dict:
        logger.info(f"Placing order → {json.dumps(payload, default=str)[:200]}")
        result = await self.execute(payload)
        logger.info(f"Order placed ✓ | result={result}")
        return result

    async def cancel_order(self, payload: dict) -> dict:
        logger.info(f"Cancelling order → {json.dumps(payload, default=str)[:200]}")
        result = await self.execute(payload)
        logger.info(f"Order cancelled ✓ | result={result}")
        return result

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def get_best_bid_ask(self, product_id: int) -> tuple[float, float]:
        """Return (best_bid, best_ask) from the orderbook."""
        book = await self.get_market_liquidity(product_id, depth=1)
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0][0]) / 1e18 if bids else 0.0
        best_ask = float(asks[0][0]) / 1e18 if asks else float("inf")
        return best_bid, best_ask

    async def get_mid_price(self, product_id: int) -> float:
        bid, ask = await self.get_best_bid_ask(product_id)
        return (bid + ask) / 2.0

    async def get_account_equity(self, sender: str) -> float:
        """Return initial health (account equity) to use as trading balance."""
        try:
            info = await self.get_subaccount_info(sender)
            healths = info.get("healths", [])
            if healths:
                # healths[0] is initial health (usable equity for new positions)
                health = healths[0].get("health", 0)
                return float(health) / 1e18
        except Exception as e:
            logger.warning(f"Could not fetch account equity: {e}")
        return 0.0


# ─── WebSocket Price Feed ──────────────────────────────────────────────────────

class NadoPriceFeed:
    """
    Subscribes to Nado's real-time best_bid_offer (BBO) feed via WebSocket.
    Calls price_callback(product_id, bid, ask, mid) on each update.
    """

    def __init__(
        self,
        product_ids: list[int],
        price_callback: Callable,
    ):
        self.product_ids    = product_ids
        self.price_callback = price_callback
        self._running       = False
        self._prices: dict[int, dict] = {}

    def get_latest(self, product_id: int) -> Optional[dict]:
        return self._prices.get(product_id)

    async def run(self):
        """Connect and stream prices, auto-reconnects on disconnect."""
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.warning(f"Price feed disconnected: {e} — reconnecting in 3s")
                await asyncio.sleep(3)

    async def stop(self):
        self._running = False

    async def _connect(self):
        async with websockets.connect(
            SUB_URL,
            ping_interval=30,  # built-in WS ping every 30s (per Nado docs)
            ping_timeout=10,
        ) as ws:
            logger.info(f"Price feed connected -> {SUB_URL}")

            # Subscribe to BBO for each product
            for pid in self.product_ids:
                sub_msg = {"method": "subscribe", "stream": {"type": "best_bid_offer", "product_id": pid}}
                await ws.send(json.dumps(sub_msg))

            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                    self._handle_message(msg)
                except Exception as e:
                    logger.debug(f"Price feed parse error: {e}")

    def _handle_message(self, msg: dict):
        mtype = msg.get("type", "")
        data  = msg.get("data", {})

        if mtype == "best_bid_offer":
            pid  = data.get("product_id")
            bid  = float(data.get("bid", 0))  / 1e18
            ask  = float(data.get("ask", 0))  / 1e18
            mid  = (bid + ask) / 2.0

            self._prices[pid] = {"bid": bid, "ask": ask, "mid": mid, "ts": time.time()}

            try:
                self.price_callback(pid, bid, ask, mid)
            except Exception as e:
                logger.error(f"price_callback error: {e}")
        else:
            # Log unknown message types to help debug
            if not hasattr(self, '_logged_types'):
                self._logged_types = set()
            if mtype not in self._logged_types:
                self._logged_types.add(mtype)
                logger.info(f"WS msg type='{mtype}' keys={list(msg.keys())} | sample={str(msg)[:200]}")
