"""日足の指標。行 = 日付、列 = 銘柄 の DataFrame (パネル) で一括計算する。

どの値も「その日の引けまでに分かる情報」だけで計算する (未来の値を使わない)。
「前日まで」の値が必要なもの (ブレイクアウト判定用の 20 日高値など) は名前に _prev を付けて shift(1) する。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close.rolling(n, min_periods=n).mean()


def atr(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """Wilder の ATR (平滑化係数 1/n)。"""
    prev = close.shift(1)
    # np.fmax は NaN を無視する → 前日終値がない日は 高値 − 安値
    tr = np.fmax(high - low, np.fmax((high - prev).abs(), (low - prev).abs()))
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rolling_max(df: pd.DataFrame, n: int) -> pd.DataFrame:
    return df.rolling(n, min_periods=n).max()


def avg(df: pd.DataFrame, n: int) -> pd.DataFrame:
    return df.rolling(n, min_periods=n).mean()


def pct_change(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close / close.shift(n) - 1.0


def compute_all(panel: dict[str, pd.DataFrame], cfg) -> dict[str, pd.DataFrame]:
    """スクリーナーと戦略が使う指標をまとめて計算する。"""
    sc, st = cfg.screener, cfg.strategy
    o, h, lo, c, v = (panel[k] for k in ("open", "high", "low", "close", "volume"))
    out = dict(panel)
    out["sma_fast"] = sma(c, sc["sma_fast"])
    out["sma_slow"] = sma(c, sc["sma_slow"])
    out["sma_trail"] = sma(c, st["trail_sma"])
    out["atr"] = atr(h, lo, c, st["atr_days"])
    out["avg_vol"] = avg(v, sc["avg_volume_days"])
    out["vol_ratio"] = avg(v, sc["volume_ratio_short"]) / avg(v, sc["volume_ratio_long"])
    out["momentum"] = pct_change(c, sc["momentum_days"])
    out["high_n"] = rolling_max(h, sc["breakout_days"])                          # 当日を含む 20 日高値 (監視リスト添付用)
    out["high_prev"] = rolling_max(h, st["breakout_days"]).shift(1)              # 前日までの 20 日高値 (ブレイク判定)
    out["avg_vol_prev"] = avg(v, st["breakout_days"]).shift(1)                   # 前日までの 20 日平均出来高
    b = panel["bench_close"]
    out["bench_sma"] = b.rolling(sc["index_sma"], min_periods=sc["index_sma"]).mean()
    return out
