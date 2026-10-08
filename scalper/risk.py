"""資金管理とキルスイッチ。

全銘柄で 1 つの RiskManager を共有し、口座全体の当日損失・取引回数・連敗を管理する。
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime

from .config import RiskConfig

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self._day: date | None = None
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self.halted_reason: str | None = None
        self.open_positions: set[str] = set()
        self._cooldown: dict[str, int] = {}

    # ---- 日次リセット ----
    def roll_day(self, t: datetime) -> None:
        d = t.date()
        if d != self._day:
            if self._day is not None:
                log.info("day %s closed: pnl=%.2f trades=%d", self._day, self.daily_pnl, self.trades_today)
            self._day = d
            self.daily_pnl = 0.0
            self.trades_today = 0
            self.consecutive_losses = 0
            self.halted_reason = None
            self._cooldown.clear()

    # ---- 足ごと ----
    def on_bar(self, code: str) -> None:
        if self._cooldown.get(code, 0) > 0:
            self._cooldown[code] -= 1

    # ---- エントリー可否 ----
    def can_open(self, code: str) -> tuple[bool, str]:
        if self.halted_reason:
            return False, self.halted_reason
        if code in self.open_positions:
            return False, "already in position"
        if len(self.open_positions) >= self.cfg.max_open_positions:
            return False, "max open positions"
        if self.trades_today >= self.cfg.max_trades_per_day:
            return False, "max trades per day"
        if self._cooldown.get(code, 0) > 0:
            return False, "cooldown after loss"
        return True, ""

    def position_size(self, price: float, stop_distance: float, lot_size: float | None = None) -> float:
        """1 トレードの損失が risk_per_trade × 口座 に収まる株数 (売買単位の倍数)。"""
        lot = float(lot_size or self.cfg.lot_size)
        if lot <= 0:
            lot = 1.0
        if price <= 0 or stop_distance <= 0:
            return 0
        risk_amount = self.cfg.account_size * self.cfg.risk_per_trade
        # 当日の残り許容損失を超えないようにする
        remaining = self.cfg.max_daily_loss + min(self.daily_pnl, 0.0)
        risk_amount = min(risk_amount, max(remaining, 0.0))
        by_risk = risk_amount / stop_distance
        by_value = self.cfg.max_position_value / price
        n = math.floor(min(by_risk, by_value) / lot + 1e-9)
        if n <= 0:
            return 0
        if lot.is_integer():
            return int(n * lot)
        decimals = max(0, -math.floor(math.log10(lot)) + 2)
        return round(n * lot, decimals)

    # ---- 状態更新 ----
    def on_open(self, code: str) -> None:
        self.open_positions.add(code)

    def on_close(self, code: str, pnl: float) -> None:
        self.open_positions.discard(code)
        self.trades_today += 1
        self.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            self._cooldown[code] = self.cfg.cooldown_bars_after_loss
        else:
            self.consecutive_losses = 0
        if self.daily_pnl <= -abs(self.cfg.max_daily_loss):
            self.halt(f"daily loss limit reached ({self.daily_pnl:.0f})")
        elif self.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.halt(f"{self.consecutive_losses} consecutive losses")

    def halt(self, reason: str) -> None:
        if not self.halted_reason:
            log.warning("TRADING HALTED for today: %s", reason)
        self.halted_reason = reason
