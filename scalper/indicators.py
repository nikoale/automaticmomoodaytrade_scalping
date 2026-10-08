"""逐次更新型のテクニカル指標。

ライブとバックテストで同じコードを使えるよう、1 本ずつ update() する形にしている。
値が揃うまでは value が None を返す。
"""
from __future__ import annotations

from collections import deque
from datetime import date


class EMA:
    def __init__(self, period: int):
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: float | None = None
        self._count = 0
        self._seed_sum = 0.0

    def update(self, x: float) -> float | None:
        self._count += 1
        if self._count < self.period:
            self._seed_sum += x
            return None
        if self._count == self.period:
            # 最初の値は SMA で初期化
            self.value = (self._seed_sum + x) / self.period
        else:
            self.value = self.alpha * x + (1 - self.alpha) * self.value
        return self.value


class RSI:
    """Wilder の RSI。"""

    def __init__(self, period: int = 14):
        self.period = period
        self.value: float | None = None
        self._prev: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self._avg_gain is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self._avg_gain = sum(self._gains) / self.period
            self._avg_loss = sum(self._losses) / self.period
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        if self._avg_loss == 0:
            self.value = 100.0 if self._avg_gain > 0 else 50.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class ATR:
    """Wilder の ATR。"""

    def __init__(self, period: int = 14):
        self.period = period
        self.value: float | None = None
        self._prev_close: float | None = None
        self._trs: list[float] = []

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        if self.value is None:
            self._trs.append(tr)
            if len(self._trs) < self.period:
                return None
            self.value = sum(self._trs) / self.period
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        return self.value


class SessionVWAP:
    """日付が変わるとリセットされる当日 VWAP。"""

    def __init__(self):
        self.value: float | None = None
        self._day: date | None = None
        self._pv = 0.0
        self._vol = 0.0

    def update(self, day: date, high: float, low: float, close: float, volume: float) -> float | None:
        if day != self._day:
            self._day = day
            self._pv = 0.0
            self._vol = 0.0
            self.value = None
        typical = (high + low + close) / 3.0
        self._pv += typical * volume
        self._vol += volume
        if self._vol > 0:
            self.value = self._pv / self._vol
        return self.value


class RollingMean:
    def __init__(self, period: int):
        self.period = period
        self._buf: deque[float] = deque(maxlen=period)
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        self._buf.append(x)
        if len(self._buf) == self.period:
            self.value = sum(self._buf) / self.period
        return self.value
