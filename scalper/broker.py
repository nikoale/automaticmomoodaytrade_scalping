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

    def commission(self, price: float, qty: float, cfg: ExecutionConfig) -> float:
        fee = cfg.commission_per_order + abs(price * qty) * cfg.commission_rate
        return min(fee, cfg.commission_max) if cfg.commission_max else fee

    # ---- 証券会社側の逆指値 (保護ストップ)。シミュレーションでは何もしない
    def protect(self, code: str, qty: float, stop: float, is_long: bool = True) -> str | None:
        """ボットが止まっても損切りされるよう、口座に逆指値注文を置く。注文 ID を返す。"""
        return None

    def update_protect(self, order_id: str, code: str, qty: float, stop: float,
                       is_long: bool = True) -> str | None:
        """逆指値の価格・数量を変更する。新しい注文 ID (変わらなければ同じ ID) を返す。"""
        return order_id

    def release_protect(self, order_id: str) -> tuple[float, float]:
        """逆指値を取り消す。取消までに約定していた (数量, 平均価格) を返す。"""
        return 0.0, 0.0

    def buying_power(self) -> float | None:
        """使える買付余力 (USD)。分からなければ None。"""
        return None

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
