# ABOUTME: Risk management for the Nado scalping bot.
# ABOUTME: All limits (position size, daily loss, risk/trade) are % of actual balance.

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from config import RISK
from strategy import Signal, TradeSignal

logger = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    product_id:  int
    signal:      Signal
    entry_price: float
    sl_price:    float
    tp_price:    float
    amount:      float          # base units (e.g. BTC)
    notional:    float          # USDT value at entry
    order_id:    Optional[str]  = None
    opened_at:   float          = field(default_factory=time.time)

    @property
    def is_long(self) -> bool:
        return self.signal == Signal.LONG

    def pnl(self, current_price: float) -> float:
        """Unrealized PnL in USDT."""
        if self.is_long:
            return (current_price - self.entry_price) * self.amount
        return (self.entry_price - current_price) * self.amount

    def should_stop_loss(self, price: float) -> bool:
        return price <= self.sl_price if self.is_long else price >= self.sl_price

    def should_take_profit(self, price: float) -> bool:
        return price >= self.tp_price if self.is_long else price <= self.tp_price


class RiskManager:
    """
    Dynamic risk manager — semua limit dihitung dari saldo aktual.

    Cara kerjanya:
      • Setiap trade, bot fetch balance terbaru dari Nado
      • max_position, daily_loss_limit, risk_per_trade → otomatis scale dengan saldo
      • Jika saldo naik → bot bisa trade lebih besar
      • Jika saldo turun → bot otomatis trade lebih kecil (proteksi drawdown)
    """

    def __init__(self, cfg=RISK):
        self.cfg              = cfg
        self.open_position:   Optional[OpenPosition] = None

        # Daily PnL tracking
        self._daily_pnl       = 0.0
        self._daily_reset_ts  = self._today_start()

        # Balance snapshot saat awal hari (untuk hitung daily limit)
        self._session_start_balance: Optional[float] = None

        # Trade history
        self._trades_log: list[dict] = []
        self._wins   = 0
        self._losses = 0

    # ─── Balance Snapshot ─────────────────────────────────────────────────────

    def update_balance_snapshot(self, balance: float):
        """
        Dipanggil saat bot start dan setiap daily reset.
        Menyimpan balance awal sebagai referensi daily loss limit.
        """
        if self._session_start_balance is None:
            self._session_start_balance = balance
            logger.info(
                f"[!] Balance snapshot | "
                f"saldo={balance:.2f} USDT | "
                f"max_per_trade={self.cfg.max_position_usdt(balance):.2f} USDT | "
                f"daily_limit=-{self.cfg.daily_loss_limit(balance):.2f} USDT | "
                f"risk/trade={self.cfg.risk_usdt(balance):.2f} USDT"
            )

    # ─── Trade Gate ───────────────────────────────────────────────────────────

    def can_trade(self, current_balance: float) -> tuple[bool, str]:
        """
        Cek apakah bot boleh buka trade baru.
        Semua limit dikalkulasi dari balance saat ini.
        """
        self._maybe_reset_daily(current_balance)

        if current_balance < self.cfg.min_balance_usdt:
            return False, f"saldo terlalu kecil ({current_balance:.2f} < {self.cfg.min_balance_usdt} USDT)"

        if self.open_position is not None:
            return False, "ada posisi terbuka"

        daily_limit = self.cfg.daily_loss_limit(
            self._session_start_balance or current_balance
        )
        if self._daily_pnl <= -daily_limit:
            return False, (
                f"daily loss limit tercapai "
                f"({self._daily_pnl:.2f} USDT ≤ -{daily_limit:.2f} USDT)"
            )

        return True, "ok"

    # ─── Position Sizing ──────────────────────────────────────────────────────

    def calc_position_size(
        self,
        balance:     float,
        entry_price: float,
        sl_price:    float,
    ) -> float:
        """
        Hitung ukuran posisi berdasarkan saldo aktual:

          risk_usdt = risk_per_trade_pct × balance
          amount    = risk_usdt / sl_distance_price
          amount    = min(amount, max_position_usdt / entry_price)

        Contoh dengan balance $500:
          risk/trade = 1% × $500 = $5
          SL distance = $150 (= 1.5 × ATR $100)
          amount = $5 / $150 = 0.0333 BTC
          max cap = $50 (10% × $500) / $83000 = 0.000602 BTC
          → final amount = 0.000602 BTC
        """
        risk_usdt    = self.cfg.risk_usdt(balance)
        max_notional = self.cfg.max_position_usdt(balance)
        sl_dist      = abs(entry_price - sl_price)

        if sl_dist == 0:
            logger.warning("SL distance = 0, tidak bisa menghitung ukuran posisi")
            return 0.0

        sized_amount = risk_usdt / sl_dist
        max_amount   = max_notional / entry_price
        amount       = min(sized_amount, max_amount)
        notional     = amount * entry_price

        logger.info(
            f"📐 Position sizing | "
            f"balance={balance:.2f} | "
            f"risk={risk_usdt:.2f} ({self.cfg.risk_per_trade_pct:.1%}) | "
            f"sl_dist={sl_dist:.4f} | "
            f"sized={sized_amount:.6f} | "
            f"cap={max_amount:.6f} ({self.cfg.max_position_pct:.0%} bal) | "
            f"→ final={amount:.6f} BTC | notional={notional:.2f} USDT"
        )
        return amount

    # ─── Open / Close ─────────────────────────────────────────────────────────

    def open_trade(
        self,
        product_id:   int,
        trade_signal: TradeSignal,
        amount:       float,
        order_id:     Optional[str] = None,
    ) -> OpenPosition:
        pos = OpenPosition(
            product_id  = product_id,
            signal      = trade_signal.signal,
            entry_price = trade_signal.entry,
            sl_price    = trade_signal.sl,
            tp_price    = trade_signal.tp,
            amount      = amount,
            notional    = amount * trade_signal.entry,
            order_id    = order_id,
        )
        self.open_position = pos
        logger.info(
            f"📂 Posisi dibuka | {trade_signal.signal.value} | "
            f"entry={pos.entry_price:.4f} | sl={pos.sl_price:.4f} | "
            f"tp={pos.tp_price:.4f} | size={amount:.6f} | "
            f"notional={pos.notional:.2f} USDT"
        )
        return pos

    def close_trade(self, exit_price: float, reason: str) -> float:
        """Tutup posisi dan return realized PnL."""
        if self.open_position is None:
            return 0.0

        pos  = self.open_position
        pnl  = pos.pnl(exit_price)
        self._daily_pnl += pnl

        if pnl >= 0:
            self._wins += 1
            result = "WIN 🏆"
        else:
            self._losses += 1
            result = "LOSS 💸"

        self._trades_log.append({
            "side":       pos.signal.value,
            "entry":      pos.entry_price,
            "exit":       exit_price,
            "size":       pos.amount,
            "notional":   round(pos.notional, 2),
            "pnl":        round(pnl, 4),
            "pnl_pct":    round(pnl / pos.notional * 100, 3) if pos.notional else 0,
            "reason":     reason,
            "duration_s": round(time.time() - pos.opened_at, 1),
            "result":     result,
        })

        logger.info(
            f"📁 Posisi ditutup | {result} | {reason} | "
            f"entry={pos.entry_price:.4f} → exit={exit_price:.4f} | "
            f"PnL={pnl:+.4f} USDT | daily_pnl={self._daily_pnl:+.4f} USDT"
        )

        self.open_position = None
        return pnl

    # ─── Stats ────────────────────────────────────────────────────────────────

    @property
    def win_rate(self) -> float:
        total = self._wins + self._losses
        return self._wins / total if total > 0 else 0.0

    @property
    def total_trades(self) -> int:
        return self._wins + self._losses

    def summary(self, current_balance: Optional[float] = None) -> dict:
        base = {
            "total_trades": self.total_trades,
            "wins":         self._wins,
            "losses":       self._losses,
            "win_rate":     f"{self.win_rate:.1%}",
            "daily_pnl":    round(self._daily_pnl, 4),
            "trades":       self._trades_log[-10:],
        }
        if current_balance is not None:
            base["risk_config"] = self.cfg.summary(current_balance)
        return base

    # ─── Internal ─────────────────────────────────────────────────────────────

    def _today_start(self) -> float:
        import datetime
        return time.mktime(datetime.date.today().timetuple())

    def _maybe_reset_daily(self, current_balance: float):
        if time.time() - self._daily_reset_ts > 86400:
            logger.info(
                f"🔄 Daily reset | "
                f"daily_pnl kemarin={self._daily_pnl:+.4f} USDT | "
                f"saldo baru={current_balance:.2f} USDT"
            )
            self._daily_pnl             = 0.0
            self._daily_reset_ts        = self._today_start()
            self._session_start_balance = current_balance  # refresh snapshot
