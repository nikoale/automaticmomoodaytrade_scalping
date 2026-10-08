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
              earnings: dict[str, list[date]] | None = None, shares: dict[str, float] | None = None,
              earnings_calendar: dict[str, list[date]] | None = None,
              names: dict[str, str] | None = None) -> ScreenResult:
    """パネル ind の i 行目 (= その日の引け) の時点で監視リストを作る。

    market_cap: 銘柄 → 時価総額 (ライブ用: 現在値)。None で shares × 終値 の近似を使う (バックテスト)
    earnings  : 銘柄 → 決算日リスト (過去 + 予定)
    earnings_calendar: 決算カレンダー (今後 N 日に決算がある銘柄 → 日付)。渡したときは earnings より優先し、
                       「カレンダーに載っていない = その期間に決算なし」とみなす (moomoo の get_earnings_calendar 用)
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
        if earnings_calendar is not None:
            fut = [x for x in earnings_calendar.get(s, []) if x > d]
            nxt, ok = (fut[0] if fut else None), True
        else:
            nxt, ok = next_earnings((earnings or {}).get(s), d)
        nexts[s] = nxt
        if not ok:
            res.unknown_earnings.append(s)
            if sc["earnings_unknown_policy"] == "exclude":
                continue
        elif nxt is not None and calendar_us.trading_days_between(d, nxt) <= sc["earnings_exclude_days"]:
            continue
        keep.append(s)
    df = df.loc[keep]
    res.counts["決算日"] = len(df)

    df = df.sort_values("vol_ratio", ascending=False).head(sc["top_n"])
    for rank, (s, r) in enumerate(df.iterrows(), 1):
        res.items.append({
            "rank": rank, "symbol": s, "name": (names or {}).get(s), "close": round(float(r["close"]), 4),
            "high_20d": round(float(r["high_n"]), 4), "atr_14": round(float(r["atr"]), 4),
            "next_earnings": nexts[s].isoformat() if nexts.get(s) else None,
            "earnings_unknown": s in res.unknown_earnings,
            "momentum_pct_rank": round(float(r["mom_pct"]), 4), "volume_ratio": round(float(r["vol_ratio"]), 4),
            "market_cap": (market_cap or {}).get(s),
        })
    return res


def clean_name(name: str) -> str:
    """Nasdaq Trader の名前から「- Common Stock」などの説明を取る (例: "Apple Inc. - Common Stock" → "Apple Inc.")。"""
    n = str(name)
    for sep in (" - ", " Common Stock", " Ordinary Shares", " Class A", " Class B", " Class C"):
        if sep in n:
            n = n.split(sep)[0]
    return n.strip().rstrip(",")


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
        log.info("[screener %s] #%d %s (%s) 終値=%.2f 20日高値=%.2f ATR=%.2f 出来高比=%.2f 6M順位=%.2f 決算=%s",
                 res.date, it["rank"], it["symbol"], it.get("name") or "", it["close"], it["high_20d"], it["atr_14"], it["volume_ratio"],
                 it["momentum_pct_rank"], it["next_earnings"])


def run_weekly(cfg, now_jst: datetime | None = None, update: bool = True) -> Path:
    """ライブの週次実行。data.screener_source で moomoo / free を切り替える。"""
    if cfg.data.screener_source == "moomoo":
        return run_weekly_moomoo(cfg, now_jst)
    return run_weekly_free(cfg, now_jst, update)


def run_weekly_moomoo(cfg, now_jst: datetime | None = None, client=None) -> Path:
    """moomoo 版の週次スクリーナー。

    1. 6 ヶ月騰落率の「市場全体の上位 20%」の境目を moomoo で求める
    2. 株価・時価総額・20 日平均出来高・騰落率・移動平均の条件をサーバー側で判定 (日足の取得枠を使わない想定)
    3. 普通株以外 (SPAC・ADR など) を名前で除外し、出来高の伸び順に上位 candidates 銘柄だけ日足を取る
    4. その日足でバックテストと同じ screen_at() にかけて確かめ直し、決算カレンダーで除外 → 上位 20
    """
    from datetime import timedelta

    from . import data
    from .indicators import compute_all
    from .moomoo_data import MoomooData
    md = cfg["moomoo_data"]
    now_jst = now_jst or datetime.now(calendar_us.TOKYO)
    last = calendar_us.last_completed_session(now_jst)
    start = (last - timedelta(days=md["bars_calendar_days"])).isoformat()
    m = client or MoomooData(cfg)
    try:
        used, remain = m.quota()
        log.info("過去 K 線の取得枠: 使用 %d / 残り %d", used, remain)
        bench = m.daily_bars(cfg.data.benchmark, start, last.isoformat())
        sma = bench["close"].rolling(cfg.screener["index_sma"]).mean()
        if cfg.strategy["index_filter"] and not (len(sma) and sma.iloc[-1] == sma.iloc[-1]
                                                 and bench["close"].iloc[-1] >= sma.iloc[-1]):
            # 市場フィルター: 候補の日足は取らない (取得枠の節約)
            res = ScreenResult(date=bench.index[-1].date() if len(bench) else last, index_ok=False,
                               note=f"市場フィルター: {cfg.data.benchmark} 終値 < {cfg.screener['index_sma']} 日線 (またはデータ不足)。"
                                    "監視リストを空にして新規停止")
            log_result(res)
            return write_watchlist(res, cfg.path("watchlists"), now_jst.date())
        threshold, total = m.momentum_threshold()
        server = m.screen_candidates(threshold)
        stocks = m.common_stocks()
        names = dict(zip(stocks["code"], stocks["name"]))
        common = [c for c in server if c["code"] in names and not data.name_excluded(names[c["code"]], cfg)]
        common.sort(key=lambda c: c["vol_ratio"] or 0, reverse=True)
        picked = common[: md["candidates"]]
        if remain < len(picked) + 1:
            log.warning("取得枠の残り (%d) が候補数より少ないので %d 銘柄に減らします", remain, max(remain - 1, 0))
            picked = picked[: max(remain - 1, 0)]
        bars = {}
        for c in picked:
            try:
                bars[c["symbol"]] = m.daily_bars(c["symbol"], start, last.isoformat())
            except Exception as e:  # noqa: BLE001 - 1 銘柄の失敗で止めない
                log.warning("日足 %s 取得失敗: %s", c["symbol"], e)
        cal = m.earnings_calendar(last + timedelta(days=1), last + timedelta(days=md["earnings_lookahead_days"]))
    finally:
        if client is None:
            m.close()
    # キャッシュに保存 (日次処理でも使う)
    data.write_prices(cfg, cfg.data.benchmark, bench, kind="live")     # 運用用 (バックテスト用とは分ける)
    for s, df in bars.items():
        if not df.empty:
            data.write_prices(cfg, s, df, kind="live")
    idx = bench.index
    panel = {k: pd.DataFrame({s: df[k].reindex(idx) for s, df in bars.items()}, index=idx)
             for k in ("open", "high", "low", "close", "volume")}
    # 「市場全体の上位 20%」の判定は moomoo 側で済んでいるので、候補はすべて条件を満たす扱い
    panel["mom_pct"] = pd.DataFrame(1.0, index=idx, columns=list(bars))
    panel["bench_close"] = bench["close"]
    ind = compute_all(panel, cfg)
    i = int(idx.searchsorted(pd.Timestamp(last), side="right")) - 1
    if i < 0 or idx[i].date() != last:
        log.warning("最新の日足が %s ではありません (取得できた最終日: %s)", last, idx[i].date() if i >= 0 else None)
    mcap = {c["symbol"]: c["market_cap"] for c in picked}
    res = screen_at(ind, i, cfg, market_cap=mcap, earnings_calendar=cal,
                    names={c["symbol"]: names.get(c["code"]) or c.get("name") for c in picked})
    res.counts = {"moomoo 条件選股": len(server), "普通株 (名前で除外後)": len(common), "日足で確認": len(bars),
                  **{f"確認: {k}": v for k, v in res.counts.items() if k != "データあり"}}
    res.note = (res.note + " " if res.note else "") + \
        f"6ヶ月騰落率の上位 {cfg.screener['momentum_top_pct']}% の境目 = {threshold:.2f}% (対象 {total} 銘柄)"
    log_result(res)
    path = write_watchlist(res, cfg.path("watchlists"), now_jst.date())
    log.info("監視リストを保存: %s (%d 銘柄)", path, len(res.items))
    return path


def run_weekly_free(cfg, now_jst: datetime | None = None, update: bool = True) -> Path:
    """無料データ版: Yahoo の日足で全銘柄を自分で計算する (バックテストと同じ方法)。"""
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
    names = {s: clean_name(n) for s, n in zip(uni["symbol"], uni["name"])}
    res = screen_at(ind, i, cfg, market_cap=mcap, earnings=earn, names=names)
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
