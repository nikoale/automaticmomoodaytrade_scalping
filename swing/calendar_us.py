"""米国株式市場 (NYSE / Nasdaq) の営業日カレンダーと、日本時間との変換。

- 祝日は NYSE の通常ルールで計算する (外部ライブラリ不要)。
  臨時休場 (例: 2025-01-09 カーター元大統領の国葬, 2018-12-05 ブッシュ元大統領) は予測できないので
  EXTRA_CLOSURES に手で足す。バックテストでは実データ (SPY の日付) を営業日として使うので影響しない。
- 夏時間/冬時間は zoneinfo (America/New_York, Asia/Tokyo) で自動的に扱う。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")

# 臨時休場 (国葬・災害など)。分かった時点で追加する
EXTRA_CLOSURES = {
    date(2001, 9, 11), date(2001, 9, 12), date(2001, 9, 13), date(2001, 9, 14),   # 同時多発テロ
    date(2004, 6, 11),                         # レーガン元大統領 国葬
    date(2007, 1, 2),                          # フォード元大統領 国葬
    date(2012, 10, 29), date(2012, 10, 30),   # ハリケーン・サンディ
    date(2018, 12, 5),                         # ブッシュ元大統領 国葬
    date(2025, 1, 9),                          # カーター元大統領 国葬
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """month の第 n weekday (月=0)。n=-1 で最終。"""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """グレゴリオ暦の復活祭 (Anonymous Gregorian algorithm)。"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date | None:
    """土曜→前の金曜、日曜→翌月曜に振替。ただし元日が土曜の場合 NYSE は振替休日を設けない。"""
    if d.weekday() == 5:
        if d.month == 1 and d.day == 1:
            return None
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def nyse_holidays(year: int) -> frozenset[date]:
    days = [
        _observed(date(year, 1, 1)),                       # New Year's Day
        _nth_weekday(year, 1, 0, 3),                       # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),                       # Washington's Birthday
        _easter(year) - timedelta(days=2),                 # Good Friday
        _nth_weekday(year, 5, 0, -1),                      # Memorial Day
        _observed(date(year, 7, 4)),                       # Independence Day
        _nth_weekday(year, 9, 0, 1),                       # Labor Day
        _nth_weekday(year, 11, 3, 4),                      # Thanksgiving
        _observed(date(year, 12, 25)),                     # Christmas
    ]
    if year >= 2022:
        days.append(_observed(date(year, 6, 19)))          # Juneteenth (2022 年から)
    # 翌年の元日が土曜の場合、その年の 12/31 (金) は休場しない (_observed が None を返す)
    out = {d for d in days if d is not None and d.year == year}
    out |= {d for d in EXTRA_CLOSURES if d.year == year}
    return frozenset(out)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_between(start: date, end: date) -> int:
    """start の翌営業日から end までの営業日数 (start < end のとき正)。end が休場日でも数え方は同じ。"""
    if end <= start:
        return 0
    n, d = 0, start
    while True:
        d = next_trading_day(d)
        if d > end:
            return n
        n += 1


def add_trading_days(d: date, n: int) -> date:
    for _ in range(n):
        d = next_trading_day(d)
    return d


def session_times_jst(d: date) -> tuple[datetime, datetime]:
    """米国の取引日 d の寄り付き・引け (9:30 / 16:00 ET) を日本時間で返す。夏時間は自動。"""
    open_ny = datetime.combine(d, time(9, 30), NY)
    close_ny = datetime.combine(d, time(16, 0), NY)
    return open_ny.astimezone(TOKYO), close_ny.astimezone(TOKYO)


def last_completed_session(now_jst: datetime) -> date:
    """日本時間 now_jst の時点で、引けまで終わっている直近の米国取引日。"""
    now_ny = now_jst.astimezone(NY)
    d = now_ny.date()
    if is_trading_day(d) and now_ny.time() >= time(16, 0):
        return d
    return prev_trading_day(d)
