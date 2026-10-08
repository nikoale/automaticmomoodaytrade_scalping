"""売買シグナルを出す戦略。

戦略は「いつ入るか」だけを決め、損切り・利確・サイズ・時間管理は engine / risk が担う。
必要なら stop_hint / target_hint で戦略固有の損切り・利確価格を提案できる。
"""
from __future__ import annotations

from datetime import date

from .indicators import ATR, EMA, RSI, RollingMean, SessionVWAP
from .models import Bar, Signal


class Strategy:
    name = "base"

    def __init__(self, atr_period: int = 14, volume_period: int = 20):
        self.atr = ATR(atr_period)
        self.vwap = SessionVWAP()
        self.avg_volume = RollingMean(volume_period)
        self.last_bar: Bar | None = None
        self._stop_hint: float | None = None
        self._target_hint: float | None = None

    # 共通指標の更新。サブクラスは super().on_bar() を最初に呼ぶ
    def on_bar(self, bar: Bar) -> Signal:
        self.atr.update(bar.high, bar.low, bar.close)
        self.vwap.update(bar.time.date(), bar.high, bar.low, bar.close, bar.volume)
        self._prev_avg_volume = self.avg_volume.value
        self.avg_volume.update(bar.volume)
        self.last_bar = bar
        self._stop_hint = None
        self._target_hint = None
        return Signal.NONE

    @property
    def ready(self) -> bool:
        return self.atr.value is not None

    def stop_hint(self) -> float | None:
        return self._stop_hint

    def target_hint(self) -> float | None:
        return self._target_hint

    def _volume_ok(self, bar: Bar, mult: float) -> bool:
        # 当該足を含まない直近平均と比較する
        avg = self._prev_avg_volume
        if mult <= 0 or avg is None:
            return True
        return bar.volume >= avg * mult


class EmaVwapMomentum(Strategy):
    """短期 EMA が長期 EMA を上抜け + 価格が VWAP より上 → 買い (下はその逆)。

    出来高が平均より多い足だけを採用し、RSI が過熱圏なら見送る。
    トレンドに乗る順張りスキャルピング。
    """
    name = "ema_vwap"

    def __init__(self, fast: int = 9, slow: int = 21, rsi_period: int = 14, rsi_long_max: float = 75,
                 rsi_short_min: float = 25, volume_mult: float = 1.2, exit_on_cross: bool = True, **kw):
        super().__init__(**kw)
        if fast >= slow:
            raise ValueError("fast must be < slow")
        self.fast, self.slow = EMA(fast), EMA(slow)
        self.rsi = RSI(rsi_period)
        self.rsi_long_max, self.rsi_short_min = rsi_long_max, rsi_short_min
        self.volume_mult = volume_mult
        self.exit_on_cross = exit_on_cross
        self._prev_diff: float | None = None

    @property
    def ready(self) -> bool:
        return super().ready and self.slow.value is not None and self.rsi.value is not None

    def on_bar(self, bar: Bar) -> Signal:
        super().on_bar(bar)
        f, s = self.fast.update(bar.close), self.slow.update(bar.close)
        r = self.rsi.update(bar.close)
        if f is None or s is None:
            return Signal.NONE
        diff = f - s
        prev, self._prev_diff = self._prev_diff, diff
        if prev is None or r is None or not self.ready:
            return Signal.NONE
        vwap = self.vwap.value
        crossed_up = prev <= 0 < diff
        crossed_down = prev >= 0 > diff
        if crossed_up and vwap is not None and bar.close > vwap and r < self.rsi_long_max \
                and self._volume_ok(bar, self.volume_mult):
            return Signal.LONG
        if crossed_down and vwap is not None and bar.close < vwap and r > self.rsi_short_min \
                and self._volume_ok(bar, self.volume_mult):
            return Signal.SHORT
        if self.exit_on_cross and (crossed_up or crossed_down):
            return Signal.EXIT
        return Signal.NONE


class OpeningRangeBreakout(Strategy):
    """寄り付き後 range_minutes 分の高値/安値をブレイクしたら順張り (ORB)。

    1 日に各方向 1 回まで。損切りはレンジの反対側 (ATR 損切りより近ければそちら)。
    """
    name = "orb"

    def __init__(self, range_minutes: int = 15, buffer_atr: float = 0.1, volume_mult: float = 1.5,
                 max_range_atr: float = 6.0, **kw):
        super().__init__(**kw)
        self.range_minutes = range_minutes
        self.buffer_atr = buffer_atr
        self.volume_mult = volume_mult
        self.max_range_atr = max_range_atr   # レンジが広すぎる日は見送る
        self._day: date | None = None
        self._first_time = None
        self._hi = self._lo = None
        self._range_done = False
        self._long_done = self._short_done = False

    def on_bar(self, bar: Bar) -> Signal:
        super().on_bar(bar)
        d = bar.time.date()
        if d != self._day:
            self._day = d
            self._first_time = bar.time
            self._hi, self._lo = bar.high, bar.low
            self._range_done = False
            self._long_done = self._short_done = False
            return Signal.NONE
        elapsed = (bar.time - self._first_time).total_seconds() / 60.0
        if not self._range_done:
            self._hi, self._lo = max(self._hi, bar.high), min(self._lo, bar.low)
            # 最初の足の確定時刻から range_minutes - 1 分経過で、range_minutes 本ぶん
            if elapsed >= self.range_minutes - 1:
                self._range_done = True
            return Signal.NONE
        atr = self.atr.value
        if atr is None or atr <= 0:
            return Signal.NONE
        if (self._hi - self._lo) > self.max_range_atr * atr:
            return Signal.NONE
        buf = self.buffer_atr * atr
        if not self._long_done and bar.close > self._hi + buf and self._volume_ok(bar, self.volume_mult):
            self._long_done = True
            self._stop_hint = self._lo
            return Signal.LONG
        if not self._short_done and bar.close < self._lo - buf and self._volume_ok(bar, self.volume_mult):
            self._short_done = True
            self._stop_hint = self._hi
            return Signal.SHORT
        return Signal.NONE

    @property
    def opening_range(self) -> tuple[float, float] | None:
        return (self._lo, self._hi) if self._range_done else None


class VwapReversion(Strategy):
    """VWAP から ATR × k 以上乖離し、RSI が極端 → VWAP 方向へ逆張り。利確目標は VWAP。

    レンジ相場向けの逆張りスキャルピング。トレンドが強い日は負けやすいので
    trend_filter (長期 EMA の傾き) で強いトレンド中は見送る。
    """
    name = "vwap_reversion"

    def __init__(self, deviation_atr: float = 2.0, rsi_period: int = 7, rsi_low: float = 20,
                 rsi_high: float = 80, trend_period: int = 50, max_trend_slope_atr: float = 0.15, **kw):
        super().__init__(**kw)
        self.deviation_atr = deviation_atr
        self.rsi = RSI(rsi_period)
        self.rsi_low, self.rsi_high = rsi_low, rsi_high
        self.trend = EMA(trend_period)
        self.max_slope = max_trend_slope_atr
        self._prev_trend: float | None = None

    def on_bar(self, bar: Bar) -> Signal:
        super().on_bar(bar)
        r = self.rsi.update(bar.close)
        t = self.trend.update(bar.close)
        prev_t, self._prev_trend = self._prev_trend, t
        atr, vwap = self.atr.value, self.vwap.value
        if None in (r, atr, vwap, t, prev_t) or atr <= 0:
            return Signal.NONE
        if abs(t - prev_t) > self.max_slope * atr:
            return Signal.NONE
        dev = (bar.close - vwap) / atr
        if dev <= -self.deviation_atr and r <= self.rsi_low:
            self._target_hint = vwap
            return Signal.LONG
        if dev >= self.deviation_atr and r >= self.rsi_high:
            self._target_hint = vwap
            return Signal.SHORT
        return Signal.NONE


STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (EmaVwapMomentum, OpeningRangeBreakout, VwapReversion)
}


def create_strategy(name: str, params: dict | None = None) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"unknown strategy {name!r}. choose from {sorted(STRATEGIES)}") from None
    return cls(**(params or {}))
