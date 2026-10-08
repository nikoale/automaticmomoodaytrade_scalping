"""足データの入出力とサンプルデータ生成。

CSV 形式: time,open,high,low,close,volume  (time は "YYYY-MM-DD HH:MM:SS"、足の確定時刻)
moomoo の request_history_kline が返す time_key もこの形式。
"""
from __future__ import annotations

import csv
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

from .models import Bar

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def parse_time(s: str) -> datetime:
    s = s.strip()
    for fmt in (TIME_FMT, "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(s)


def load_csv(path: str | Path) -> list[Bar]:
    bars = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            t = row.get("time") or row.get("time_key")
            bars.append(Bar(parse_time(t), float(row["open"]), float(row["high"]), float(row["low"]),
                            float(row["close"]), float(row.get("volume") or 0)))
    bars.sort(key=lambda b: b.time)
    return bars


def save_csv(bars: list[Bar], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.time.strftime(TIME_FMT), b.open, b.high, b.low, b.close, int(b.volume)])


def resample(bars: list[Bar], minutes: int) -> list[Bar]:
    """1 分足を N 分足にまとめる (確定時刻基準)。"""
    if minutes <= 1:
        return list(bars)
    out: list[Bar] = []
    bucket: list[Bar] = []
    key = None
    for b in bars:
        # 確定時刻 09:01..09:05 を 09:05 の足にまとめる
        m = b.time.hour * 60 + b.time.minute
        end_m = math.ceil(m / minutes) * minutes
        k = (b.time.date(), end_m)
        if key is not None and k != key:
            out.append(_merge(bucket, key))
            bucket = []
        key = k
        bucket.append(b)
    if bucket:
        out.append(_merge(bucket, key))
    return out


def _merge(bucket: list[Bar], key) -> Bar:
    d, end_m = key
    t = datetime(d.year, d.month, d.day) + timedelta(minutes=end_m)
    return Bar(t, bucket[0].open, max(b.high for b in bucket), min(b.low for b in bucket),
               bucket[-1].close, sum(b.volume for b in bucket))


def generate_sample(days: int = 5, start_price: float = 3000.0, market: str = "JP", seed: int = 42,
                    start_date: datetime | None = None) -> list[Bar]:
    """動作確認用の擬似 1 分足 (ランダムウォーク + 日中のトレンド/レンジ局面)。実相場ではない。"""
    rng = random.Random(seed)
    if market.upper() == "JP":
        sessions = [((9, 0), (11, 30)), ((12, 30), (15, 30))]
        tick = 1.0
    else:
        sessions = [((9, 30), (16, 0))]
        tick = 0.01
    day = (start_date or datetime(2026, 1, 5)).replace(hour=0, minute=0, second=0, microsecond=0)
    price = start_price
    bars: list[Bar] = []
    made = 0
    while made < days:
        if day.weekday() < 5:
            drift = 0.0
            vol = start_price * 0.0015
            for (sh, sm), (eh, em) in sessions:
                t = day.replace(hour=sh, minute=sm)
                end = day.replace(hour=eh, minute=em)
                while t < end:
                    t += timedelta(minutes=1)
                    if rng.random() < 0.03:  # 局面の切り替え
                        drift = rng.choice([-1, 0, 0, 1]) * vol * 0.25
                    ret = drift + rng.gauss(0, vol)
                    o = price
                    c = max(tick, o + ret)
                    hi = max(o, c) + abs(rng.gauss(0, vol * 0.5))
                    lo = min(o, c) - abs(rng.gauss(0, vol * 0.5))
                    minutes_from_open = (t - day.replace(hour=sh, minute=sm)).seconds / 60
                    v = int(rng.lognormvariate(8, 0.5) * (2.5 if minutes_from_open < 15 else 1.0)
                            * (1 + abs(ret) / vol * 0.5))
                    r = lambda x: round(round(x / tick) * tick, 4)  # noqa: E731
                    bars.append(Bar(t, r(o), r(hi), r(max(lo, tick)), r(c), v))
                    price = c
            made += 1
        day += timedelta(days=1)
    return bars
