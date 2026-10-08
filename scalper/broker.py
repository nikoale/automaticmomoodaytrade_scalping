"""発注インターフェースとシミュレーション約定。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from .config import ExecutionConfig
from .market import round_to_tick, tick_size
from .models import Fill, Side


class Broker(ABC):
    @abstractmethod
    def execute(self, code: str, side: Side, qty: int, ref_price: float, t: datetime,
                reason: str = "", exact: bool = False) -> Fill | None:
        """qty 株を side 方向に約定させる。ref_price は判断時点の価格。

        exact=True はシミュレーションで ref_price ちょうどに約定させる (指値利確の再現用)。

        約定しなければ None、部分約定なら qty < 要求数量 の Fill を返す。
        """

    def commission(self, price: float, qty: int, cfg: ExecutionConfig) -> float:
        return cfg.commission_per_order + abs(price * qty) * cfg.commission_rate

    def close(self) -> None:
        pass


class SimBroker(Broker):
    """バックテスト / paper 用。ref_price から slippage_ticks 不利な価格で即時全約定する。"""

    def __init__(self, cfg: ExecutionConfig, market: str):
        self.cfg = cfg
        self.market = market
        self.fills: list[tuple[str, Fill]] = []

    def execute(self, code, side, qty, ref_price, t, reason="", exact=False):
        if qty <= 0:
            return None
        tick = tick_size(ref_price, self.market, self.cfg.tick_size)
        slip = 0.0 if exact else self.cfg.slippage_ticks * tick
        if side == Side.BUY:
            price = round_to_tick(ref_price + slip, tick, "up")
        else:
            price = round_to_tick(ref_price - slip, tick, "down")
        fill = Fill(side, qty, price, t, self.commission(price, qty, self.cfg))
        self.fills.append((code, fill))
        return fill
