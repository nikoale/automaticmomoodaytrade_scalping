"""フェーズ 1: 週次スクリーナー。

毎週土曜 (日本時間の午前) に 1 回、直近の金曜の引けまでのデータで監視リストを作る。
同じ関数 screen_at() をバックテストでも毎週呼ぶので、ライブとバックテストで条件がずれない。

条件 (すべて config.yaml の screener):
  母集団 : 普通株 / 株価 10〜100 ドル / 20 日平均出来高 50 万株以上 / 時価総額 3 億ドル以上 /
           次回決算が今後 15 営業日以内なら除外
  トレンド: 終値 > 50 日線 > 200 日線
  ランク : (1) 6 ヶ月上昇率が市場全体の上位 20%  (2) 出来高の伸び (20 日平均 / 100 日平均) の大きい順
  出力   : 上位 20 銘柄。SPY が 200 日線を下回る週は空 (新規停止)
"""
from __future__ import annotations

import bisect
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from . import calendar_us

log = logging.getLogger(__name__)

# 決算日データがこれより先まで空いていたら「次回が不明」とみなす (四半期決算なので通常 100 日以内)
EARNINGS_GAP_DAYS = 120


@dataclass
class ScreenResult:
    date: date
    index_ok: bool
    items: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)       # 各段階で残った銘柄数
    unknown_earnings: list[str] = field(default_factory=list)
    note: str = ""


def next_earnings(dates: list[date] | None, d: date) -> tuple[date | None, bool]:
    """d より後の次回決算日と、それが信頼できるか (False = 不明扱い)。"""
    if not dates:
        return None, False
    i = bisect.bisect_right(dates, d)
    if i >= len(dates):
        return None, False                          # 予定日が未登録
    nxt = dates[i]
    prev_ok = i > 0 and (d - dates[i - 1]).days <= EARNINGS_GAP_DAYS + 30
    if (nxt - d).days > EARNINGS_GAP_DAYS or not prev_ok:
        return nxt, False                           # 途中の決算が抜けている可能性
    return nxt, True


def _f(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def screen_at(ind: dict, i: int, cfg, market_cap: dict[str, float] | None = None,
              earnings: dict[str, list[date]] | None = None, shares: dict[str, float] | None = None) -> ScreenResult:
    """パネル ind の i 行目 (= その日の引け) の時点で監視リストを作る。

    market_cap: 銘柄 → 時価総額 (ライブ用: 現在値)。None で shares × 終値 の近似を使う (バックテスト)
    earnings  : 銘柄 → 決算日リスト (過去 + 予定)
    """
    sc = cfg.screener
    idx = ind["close"].index
    d = idx[i].date()
    res = ScreenResult(date=d, index_ok=True)
    b, bs = _f(ind["bench_close"].iloc[i]), _f(ind["bench_sma"].iloc[i])
    if cfg.strategy["index_filter"] and (b is None or bs is None or b < bs):
        res.index_ok = False
        res.note = (f"市場フィルター: {cfg.data.benchmark} 終値 {b} < {sc['index_sma']} 日線 {bs}。監視リストを空にして新規停止"
                    if b is not None and bs is not None else "市場フィルター: 指数データ不足のため新規停止")
        return res

    row = {k: ind[k].iloc[i] for k in ("close", "avg_vol", "sma_fast", "sma_slow", "mom_pct", "vol_ratio", "high_n", "atr")}
    df = pd.DataFrame(row)
    res.counts["データあり"] = int(df["close"].notna().sum())
    df = df[(df["close"] >= sc["price_min"]) & (df["close"] <= sc["price_max"])]
    res.counts["株価"] = len(df)
    df = df[df["avg_vol"] >= sc["avg_volume_min"]]
    res.counts["出来高"] = len(df)

    # 時価総額
    if market_cap is not None:
        mc = pd.Series({s: market_cap.get(s) for s in df.index}, dtype="float64")
        df = df[mc.reindex(df.index) >= sc["market_cap_min_usd"]]
    elif shares is not None:
        sh = pd.Series({s: shares.get(s) for s in df.index}, dtype="float64")
        df = df[(sh.reindex(df.index) * df["close"]) >= sc["market_cap_min_usd"]]
    res.counts["時価総額"] = len(df)

    df = df[(df["close"] > df["sma_fast"]) & (df["sma_fast"] > df["sma_slow"])]
    res.counts["トレンド"] = len(df)
    df = df[df["mom_pct"] >= 1.0 - sc["momentum_top_pct"] / 100.0]
    res.counts["6ヶ月上昇率 上位"] = len(df)

    # 決算日 (候補が絞れてから見る)
    keep, nexts = [], {}
    for s in df.index:
        nxt, ok = next_earnings((earnings or {}).get(s), d)
        nexts[s] = nxt
        if not ok:
            res.unknown_earnings.append(s)
            if sc["earnings_unknown_policy"] == "exclude":
                continue
        elif calendar_us.trading_days_between(d, nxt) <= sc["earnings_exclude_days"]:
            continue
        keep.append(s)
    df = df.loc[keep]
    res.counts["決算日"] = len(df)

    df = df.sort_values("vol_ratio", ascending=False).head(sc["top_n"])
    for rank, (s, r) in enumerate(df.iterrows(), 1):
        res.items.append({
            "rank": rank, "symbol": s, "close": round(float(r["close"]), 4),
            "high_20d": round(float(r["high_n"]), 4), "atr_14": round(float(r["atr"]), 4),
            "next_earnings": nexts[s].isoformat() if nexts.get(s) else None,
            "earnings_unknown": s in res.unknown_earnings,
            "momentum_pct_rank": round(float(r["mom_pct"]), 4), "volume_ratio": round(float(r["vol_ratio"]), 4),
            "market_cap": (market_cap or {}).get(s),
        })
    return res


def write_watchlist(res: ScreenResult, out_dir: Path, run_date: date) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"watchlist_{run_date:%Y%m%d}.json"
    payload = {"run_date": run_date.isoformat(), "data_date": res.date.isoformat(), "index_ok": res.index_ok,
               "note": res.note, "counts": res.counts, "unknown_earnings": res.unknown_earnings,
               "items": res.items}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_watchlist(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def log_result(res: ScreenResult) -> None:
    if not res.index_ok:
        log.warning("[screener %s] %s", res.date, res.note)
        return
    log.info("[screener %s] 絞り込み: %s", res.date, " → ".join(f"{k} {v}" for k, v in res.counts.items()))
    if res.unknown_earnings:
        log.warning("[screener %s] 決算日が取得できない銘柄 (要確認): %s", res.date, ", ".join(res.unknown_earnings))
    for it in res.items:
        log.info("[screener %s] #%d %s 終値=%.2f 20日高値=%.2f ATR=%.2f 出来高比=%.2f 6M順位=%.2f 決算=%s",
                 res.date, it["rank"], it["symbol"], it["close"], it["high_20d"], it["atr_14"], it["volume_ratio"],
                 it["momentum_pct_rank"], it["next_earnings"])


def run_weekly(cfg, now_jst: datetime | None = None, update: bool = True) -> Path:
    """ライブの週次実行: データ更新 → スクリーニング → watchlist_YYYYMMDD.json。"""
    from . import data
    from .indicators import compute_all
    now_jst = now_jst or datetime.now(calendar_us.TOKYO)
    last = calendar_us.last_completed_session(now_jst)
    uni = data.load_universe(cfg)
    symbols = list(uni["symbol"])
    if update:
        data.update_prices(cfg, symbols + [cfg.data.benchmark])
    start = (pd.Timestamp(last) - pd.Timedelta(days=550)).strftime("%Y-%m-%d")
    panel = data.load_panel(cfg, symbols, start=start)
    ind = compute_all(panel, cfg)
    i = int(ind["close"].index.searchsorted(pd.Timestamp(last), side="right")) - 1
    if ind["close"].index[i].date() != last:
        log.warning("最新の日足が %s ではなく %s です (データ未更新の可能性)", last, ind["close"].index[i].date())
    # 時価総額・決算日は候補が絞れてから取得する (API 呼び出しを減らすため)
    index_ok = screen_at(ind, i, cfg, earnings={}).index_ok
    cand = _pre_candidates(ind, i, cfg) if index_ok else []
    if update and cand:
        data.update_meta(cfg, cand)
        data.update_earnings(cfg, cand)
    meta = data.load_meta(cfg)
    mcap = {s: _f(meta["market_cap"].get(s)) for s in cand if s in meta.index}
    earn = {s: data.read_earnings(cfg, s) for s in cand}
    res = screen_at(ind, i, cfg, market_cap=mcap, earnings=earn)
    log_result(res)
    path = write_watchlist(res, cfg.path("watchlists"), now_jst.date())
    log.info("監視リストを保存: %s (%d 銘柄)", path, len(res.items))
    return path


def _pre_candidates(ind: dict, i: int, cfg) -> list[str]:
    """時価総額・決算日を見る前の候補 (株価・出来高・トレンド・上昇率)。"""
    sc = cfg.screener
    c = ind["close"].iloc[i]
    m = ((c >= sc["price_min"]) & (c <= sc["price_max"]) & (ind["avg_vol"].iloc[i] >= sc["avg_volume_min"])
         & (c > ind["sma_fast"].iloc[i]) & (ind["sma_fast"].iloc[i] > ind["sma_slow"].iloc[i])
         & (ind["mom_pct"].iloc[i] >= 1.0 - sc["momentum_top_pct"] / 100.0))
    return list(m[m].index)
