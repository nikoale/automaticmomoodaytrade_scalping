"""共通データ構造。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Signal(str, Enum):
    NONE = "NONE"
    LONG = "LONG"     # 新規買い
    SHORT = "SHORT"   # 新規売り（空売り）
    EXIT = "EXIT"     # 戦略側からの手仕舞い要求


@dataclass(frozen=True)
class Bar:
    """確定済みの足 (OHLCV)。time は足の確定時刻 = 終了時刻（取引所ローカル時刻）。

    moomoo の分足 time_key と同じ規約 (例: 09:01 の足 = 09:00〜09:01)。
    """
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Fill:
    side: Side
    qty: int
    price: float
    time: datetime
    commission: float = 0.0


@dataclass
class Position:
    """エンジンが管理する保有ポジション。qty は正=ロング、負=ショート。"""
    qty: int
    entry_price: float
    entry_time: datetime
    stop: float
    target: float
    bars_held: int = 0
    best_price: float = 0.0   # トレーリング用：建玉後の最有利価格
    entry_commission: float = 0.0
    atr: float = 0.0          # エントリー時の ATR
    reason: str = ""

    @property
    def is_long(self) -> bool:
        return self.qty > 0


@dataclass
class Trade:
    """決済済みトレードの記録。"""
    code: str
    direction: str
    qty: int
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    pnl: float            # 手数料控除後
    reason: str
    bars_held: int = 0
    extra: dict = field(default_factory=dict)
