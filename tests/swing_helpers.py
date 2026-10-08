"""スイング版テスト用: 手で作った値動きのパネル。"""
import numpy as np
import pandas as pd

from swing import config


def cfg(**over):
    base = {"screener": {"earnings_unknown_policy": "keep"}, "backtest": {"start": "2020-01-01", "end": None}}
    for k, v in over.items():
        base.setdefault(k, {}).update(v)
    return config.load(overrides=base)


def fillers(n_days: int, k: int = 20) -> dict[str, dict]:
    """市場全体の上昇率順位を自然にするための、ほぼ横ばいの「その他大勢」の銘柄。"""
    t = np.arange(n_days)
    # ゆるやかに下落 + 小さな波 → トレンド条件 (終値 > 50 日線 > 200 日線) を満たさない
    return {f"Z{i:02d}": {"close": 30 * (1 - 0.0008 * t) * (1 + 0.01 * np.sin(t / 5 + i)),
                          "volume": np.full(n_days, 1_000_000.0)} for i in range(k)}


def make_panel(series: dict[str, dict], dates: pd.DatetimeIndex, bench: np.ndarray | None = None,
               add_fillers: bool = True) -> dict:
    """series: 銘柄 → {"close": 配列, 任意で "open"/"high"/"low"/"volume"}。指定がなければ
    open = 前日終値, high/low = max/min(open, close) ± 0.2%, volume = 100 万株。"""
    out = {k: {} for k in ("open", "high", "low", "close", "volume")}
    if add_fillers:
        series = {**fillers(len(dates)), **series}
    for s, d in series.items():
        c = np.asarray(d["close"], dtype=float)
        o = np.asarray(d.get("open", np.r_[c[0], c[:-1]]), dtype=float)
        hi = np.asarray(d.get("high", np.maximum(o, c) * 1.002), dtype=float)
        lo = np.asarray(d.get("low", np.minimum(o, c) * 0.998), dtype=float)
        vol = np.asarray(d.get("volume", np.full(len(c), 1_000_000.0)), dtype=float)
        for k, a in (("open", o), ("high", hi), ("low", lo), ("close", c), ("volume", vol)):
            out[k][s] = a
    panel = {k: pd.DataFrame(v, index=dates) for k, v in out.items()}
    c = panel["close"]
    panel["mom_pct"] = (c / c.shift(126) - 1).rank(axis=1, pct=True)
    b = bench if bench is not None else np.linspace(300, 400, len(dates))
    panel["bench_close"] = pd.Series(b, index=dates)
    return panel


def trend_with_breakout(n: int, breakout_at: int, start=20.0, end=40.0, jump=1.06, vol_mult=3.0):
    """ゆるやかな上昇 → breakout_at 日目に 20 日高値を上抜け (出来高も急増)。"""
    c = np.linspace(start, end, n)
    # ブレイク前 25 日は横ばいにして、20 日高値をはっきりさせる
    flat = c[breakout_at - 25]
    c[breakout_at - 25:breakout_at] = flat
    c[breakout_at:] = c[breakout_at:] - c[breakout_at] + flat * jump
    vol = np.full(n, 1_000_000.0)
    vol[breakout_at] = 1_000_000.0 * vol_mult
    return {"close": c, "volume": vol}
