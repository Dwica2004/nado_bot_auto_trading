# ABOUTME: EIP712 signing utilities for Nado order execution.
# ABOUTME: Handles wallet setup, sender encoding, and order signing.

import time
import struct
import logging
from eth_account import Account
from eth_account.messages import encode_typed_data

from config import (
    ORDER_TYPES, CANCEL_TYPES, EIP712_DOMAIN_TEMPLATE,
    CHAIN_ID_TESTNET, CHAIN_ID_MAINNET,
    PRIVATE_KEY, WALLET_ADDRESS, SUBACCOUNT_NAME,
    USE_TESTNET
)

logger = logging.getLogger(__name__)

SCALE_18 = 10 ** 18  # 1e18 — all amounts & prices use this scale

# ─── Sender Encoding ──────────────────────────────────────────────────────────

def build_sender(address: str, subaccount: str = "") -> bytes:
    """
    Nado sender = bytes32: address (20 bytes) + subaccount (12 bytes, null-padded).
    e.g. 0x7a5ec2...d7c43 + 'default' → 0x7a5ec2...d7c4364656661756c740000000000
    """
    addr_bytes = bytes.fromhex(address.lower().removeprefix("0x"))
    if len(addr_bytes) != 20:
        raise ValueError(f"Invalid address length: {len(addr_bytes)}")

    if not subaccount or not subaccount.strip():
        subaccount = "default"

    # Subaccount: up to 12 ASCII bytes, zero-padded
    sub_bytes = subaccount.encode("ascii")[:12].ljust(12, b"\x00")
    return addr_bytes + sub_bytes


def build_sender_hex(address: str, subaccount: str = "") -> str:
    """Return sender as 0x-prefixed hex string."""
    return "0x" + build_sender(address, subaccount).hex()


# ─── Nonce ────────────────────────────────────────────────────────────────────

def generate_nonce() -> int:
    """Generate a unique nonce based on current time (milliseconds) shifted for Nado time embedding."""
    import random
    ts_ms = int(time.time() * 1000) + 60000  # 60s window for transit latency
    return (ts_ms << 20) | random.randint(0, 0xFFFFF)


# ─── Price / Amount Encoding ──────────────────────────────────────────────────

def to_x18(value: float) -> int:
    """Convert a float price/amount to x18 integer (1e18 precision)."""
    return int(value * SCALE_18)


def from_x18(value: int) -> float:
    """Convert x18 integer back to float."""
    return value / SCALE_18


# ─── EIP712 Order Signing ─────────────────────────────────────────────────────

class NadoSigner:
    """Handles EIP712 signing of Nado orders and cancellations."""

    def __init__(self, private_key: str, endpoint_addr: str, testnet: bool = True):
        self.account    = Account.from_key(private_key)
        self.address    = self.account.address
        self.chain_id   = CHAIN_ID_TESTNET if testnet else CHAIN_ID_MAINNET
        self.endpoint   = endpoint_addr   # verifyingContract for orders

        # Cancellations use a different contract (the endpoint)
        self.domain_order = {
            **EIP712_DOMAIN_TEMPLATE,
            "chainId": self.chain_id,
            "verifyingContract": "0x0000000000000000000000000000000000000001",
        }
        self.domain_cancel = {
            **EIP712_DOMAIN_TEMPLATE,
            "chainId": self.chain_id,
            "verifyingContract": endpoint_addr,
        }

        logger.info(f"Signer initialized | address={self.address} | testnet={testnet}")

    def sign_order(
        self,
        product_id:  int,
        price:       float,
        amount:      float,   # positive = BUY, negative = SELL
        expiration:  int = None,
        subaccount:  str = "",
        post_only:   bool = False,
    ) -> dict:
        """
        Build and sign a place_order execute payload.

        Returns full payload ready to POST to /execute.
        """
        if expiration is None:
            expiration = int(time.time()) + 300  # 5 minutes TTL to avoid recv_time rejects

        sender_hex = build_sender_hex(self.address, subaccount)
        sender_bytes = build_sender(self.address, subaccount)

        price_x18  = to_x18(price)
        amount_x18 = to_x18(amount)
        nonce      = generate_nonce()

        # appendix: version=1 (bits 0-7), post_only flag (bit 8)
        appendix = 1 | (256 if post_only else 0)

        message = {
            "sender":     sender_bytes,
            "priceX18":   price_x18,
            "amount":     amount_x18,
            "expiration": expiration,
            "nonce":      nonce,
        }

        structured = {
            "types":       ORDER_TYPES,
            "primaryType": "Order",
            "domain":      self.domain_order,
            "message":     message,
        }

        signable   = encode_typed_data(full_message=structured)
        signed     = self.account.sign_message(signable)
        signature  = signed.signature.hex()

        payload = {
            "place_order": {
                "product_id": product_id,
                "order": {
                    "sender":     sender_hex,
                    "priceX18":   str(price_x18),
                    "amount":     str(amount_x18),
                    "expiration": str(expiration),
                    "nonce":      str(nonce),
                    "appendix":   str(appendix),
                },
                "signature": "0x" + signature,
                "id": nonce % 100000  # client-side correlation id
            }
        }

        logger.debug(
            f"Order signed | product={product_id} | "
            f"{'BUY' if amount > 0 else 'SELL'} {abs(amount):.6f} @ {price:.2f} | "
            f"nonce={nonce}"
        )
        return payload

    def sign_cancel(self, product_id: int, digest: str, subaccount: str = "") -> dict:
        """Sign a cancellation for a specific order digest."""
        sender_hex   = build_sender_hex(self.address, subaccount)
        sender_bytes = build_sender(self.address, subaccount)
        nonce        = generate_nonce()

        digest_bytes = bytes.fromhex(digest.removeprefix("0x"))

        message = {
            "sender":     sender_bytes,
            "productIds": [product_id],
            "digests":    [digest_bytes],
            "nonce":      nonce,
        }

        structured = {
            "types":       CANCEL_TYPES,
            "primaryType": "Cancellation",
            "domain":      self.domain_cancel,
            "message":     message,
        }

        signable  = encode_typed_data(full_message=structured)
        signed    = self.account.sign_message(signable)
        signature = signed.signature.hex()

        return {
            "cancel_orders": {
                "product_id": product_id,
                "sender":     sender_hex,
                "digests":    [digest],
                "nonce":      str(nonce),
                "signature":  "0x" + signature,
            }
        }
