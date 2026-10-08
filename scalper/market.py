"""米国株の市場ルール: 呼値 (ティックサイズ) と取引時間。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

def tick_size(price: float, market: str = "US", setting="auto") -> float:
    """価格に対する呼値。setting が数値ならその値、"auto" なら米国株の刻み (1 ドル以上 0.01 / 未満 0.0001)。"""
    if isinstance(setting, (int, float)) and not isinstance(setting, bool):
        return float(setting)
    return 0.01 if price >= 1.0 else 0.0001


def round_to_tick(price: float, tick: float, direction: str = "nearest") -> float:
    """呼値に丸める。direction: "up" / "down" / "nearest"。"""
    mode = {"up": ROUND_CEILING, "down": ROUND_FLOOR}.get(direction, ROUND_HALF_UP)
    t = Decimal(str(tick))
    # 浮動小数点の誤差 (3000.0000000004 等) で 1 ティックずれないよう先に丸める
    n = (Decimal(str(price)) / t).quantize(Decimal("1e-6"))
    return float(n.quantize(Decimal(1), rounding=mode) * t)


class TradingSessions:
    """取引時間帯の判定。時刻はすべて取引所ローカルの naive datetime を前提とする。"""

    def __init__(self, sessions: list[tuple[time, time]], no_entry_first_minutes: int = 0,
                 no_entry_last_minutes: int = 0, flatten_before_close_minutes: int = 0,
                 flatten_each_session: bool = True, day_rollover: time = time(0, 0)):
        if not sessions:
            raise ValueError("sessions must not be empty")
        self.day_offset = timedelta(hours=day_rollover.hour, minutes=day_rollover.minute)
        # 取引日の中での順番 (day_rollover 起点) に並べる。最後のセッションが「大引け」
        off = day_rollover.hour * 60 + day_rollover.minute
        self.sessions = sorted(sessions, key=lambda se: (se[0].hour * 60 + se[0].minute - off) % 1440)
        self.no_entry_first = timedelta(minutes=no_entry_first_minutes)
        self.no_entry_last = timedelta(minutes=no_entry_last_minutes)
        self.flatten_before = timedelta(minutes=flatten_before_close_minutes)
        self.flatten_each_session = flatten_each_session

    @classmethod
    def from_config(cls, sc) -> "TradingSessions":
        h, m = sc.day_rollover.split(":")
        return cls(sc.parsed_sessions(), sc.no_entry_first_minutes, sc.no_entry_last_minutes,
                   sc.flatten_before_close_minutes, sc.flatten_each_session, time(int(h), int(m)))

    def trading_day(self, t: datetime) -> date:
        """t が属する取引日 (day_rollover 時刻で日付が切り替わる)。"""
        return (t - self.day_offset).date()

    def _current(self, t: datetime) -> tuple[int, datetime, datetime] | None:
        for i, (s, e) in enumerate(self.sessions):
            # 終了 <= 開始 のセッションは日付をまたぐ (例: オーバーナイト 20:00〜04:00)
            for d in (t.date(), t.date() - timedelta(days=1)):
                start = datetime.combine(d, s)
                end = datetime.combine(d + timedelta(days=1) if e <= s else d, e)
                # 足の確定時刻ベースなので start < t <= end をセッション内とみなす
                if start < t <= end:
                    return i, start, end
        return None

    def in_session(self, t: datetime) -> bool:
        return self._current(t) is not None

    def can_enter(self, t: datetime) -> bool:
        cur = self._current(t)
        if cur is None:
            return False
        _, start, end = cur
        if t - start < self.no_entry_first:
            return False
        if end - t <= self.no_entry_last:
            return False
        return not self.must_flatten(t)

    def must_flatten(self, t: datetime) -> bool:
        """ポジションを強制決済すべき時刻か。"""
        cur = self._current(t)
        if cur is None:
            return True
        idx, _, end = cur
        is_last = idx == len(self.sessions) - 1
        if is_last or self.flatten_each_session:
            return end - t <= self.flatten_before
        return False

    def session_index(self, t: datetime) -> int | None:
        cur = self._current(t)
        return None if cur is None else cur[0]
