"""バックテスト結果レポート (Markdown + 図 + CSV)。

作るもの (reports/<日時>/):
  report.md            評価指標・年別表・期間別・指数フィルター有無の比較・注意事項
  equity.png           損益曲線 (指数フィルター あり / なし) と SPY
  drawdown.png         ドローダウン
  periods.png          検証期間 / 検証外期間 / 2020 年 / 2022 年 の損益曲線
  trades_*.csv         全トレード
  decisions_full.csv   判断ログ (候補・エントリー・損切り更新・手仕舞い・見送り)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import backtest

log = logging.getLogger(__name__)


def _fmt(v, nd=1):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    return str(v)


def run_suite(cfg, ind, earnings, shares) -> dict:
    """指示書どおりの組み合わせをすべて回す。各期間は資金 20 万円から独立に開始する。"""
    dates = ind["close"].index
    start = pd.Timestamp(cfg.backtest["start"])
    end = pd.Timestamp(cfg.backtest["end"]) if cfg.backtest["end"] else dates[-1]
    oos_start = end - pd.DateOffset(years=cfg.backtest["out_of_sample_years"])
    runs = {}

    def go(key, s, e, flt, label):
        log.info("バックテスト: %s (%s 〜 %s, 指数フィルター=%s)", label, s.date(), e.date(), flt)
        runs[key] = backtest.run(cfg, ind, earnings, shares, start=str(s.date()), end=str(e.date()),
                                 index_filter=flt, label=label, record_decisions=(key == "full_on"))

    go("full_on", start, end, True, "全期間 (指数フィルターあり)")
    go("full_off", start, end, False, "全期間 (指数フィルターなし)")
    go("is_on", start, oos_start - pd.Timedelta(days=1), True, "検証期間")
    go("oos_on", oos_start, end, True, f"検証外期間 (直近 {cfg.backtest['out_of_sample_years']} 年)")
    for n, (name, (s, e)) in enumerate(cfg.backtest["stress_periods"].items()):
        s, e = pd.Timestamp(s), pd.Timestamp(e)
        if s < dates[0] or s > end:
            continue
        key = re.sub(r"[^0-9A-Za-z]", "", name) or f"stress{n}"     # 図・ファイル名は英数字だけ
        go(f"{key}_on", s, min(e, end), True, f"{name} (指数フィルターあり)")
        go(f"{key}_off", s, min(e, end), False, f"{name} (指数フィルターなし)")
    # 寄り付きの買い方の比較 (全期間・指数フィルターあり)。キーは gap_ で始める (主要な表とは別に出す)
    for g in cfg.backtest["entry_gap_compare"]:
        label = "成行（上限なし）" if g is None else f"上限 +{g:g}% の指値"
        log.info("バックテスト: 寄り付きの買い方 = %s", label)
        runs[gap_key(g)] = backtest.run(cfg, ind, earnings, shares, start=str(start.date()), end=str(end.date()),
                                        index_filter=True, label=label, record_decisions=False, entry_gap_pct=g)
    return runs


INDICATOR_KEYS = {"breakout_days", "trail_sma", "atr_days"}     # 変えると指標の計算し直しが要る


def run_variants(cfg, ind, earnings, shares) -> dict:
    """改善案ごとに 検証期間 / 検証外期間 を回す (指数フィルターあり)。"""
    import copy

    from .indicators import compute_all
    dates = ind["close"].index
    start = pd.Timestamp(cfg.backtest["start"])
    end = pd.Timestamp(cfg.backtest["end"]) if cfg.backtest["end"] else dates[-1]
    oos = end - pd.DateOffset(years=cfg.backtest["out_of_sample_years"])
    out = {}
    for n, v in enumerate(cfg.backtest["variants"] or []):
        c = copy.deepcopy(cfg)
        for sec in ("strategy", "risk", "screener"):
            for k, val in (v.get(sec) or {}).items():
                if k not in c[sec]:
                    raise ValueError(f"改善案「{v['name']}」の {sec}.{k} は config にありません")
                c[sec][k] = val
        need = INDICATOR_KEYS & set((v.get("strategy") or {}))
        ind_v = compute_all({k: ind[k] for k in ("open", "high", "low", "close", "volume", "bench_close", "mom_pct") if k in ind}, c) \
            if need else ind
        log.info("改善案 %d: %s", n, v["name"])
        res = {}
        for key, s, e in (("is", start, oos - pd.Timedelta(days=1)), ("oos", oos, end)):
            r = backtest.run(c, ind_v, earnings, shares, start=str(s.date()), end=str(e.date()), index_filter=True,
                             label=v["name"], record_decisions=False)
            res[key] = {"stats": backtest.stats(r), "diag": backtest.diagnose(r, c.fees["slippage_pct"], c.strategy["initial_stop_atr"])}
        out[f"v{n}"] = {"name": v["name"], "why": v.get("why", ""), "strategy": v.get("strategy") or {}, "risk": v.get("risk") or {},
                        "screener": v.get("screener") or {}, **res}
    return out


def gap_key(g) -> str:
    return "gap_market" if g is None else "gap_" + f"{g:g}".replace(".", "_")


def write(cfg, runs: dict, ind: dict, notes: list[str], data_info: dict, variants: dict | None = None) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = cfg.report_dir() / datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    fx = cfg.account["fx_rate_jpy_per_usd"]
    gap_runs = {k: r for k, r in runs.items() if k.startswith("gap_")}
    runs = {k: r for k, r in runs.items() if not k.startswith("gap_")}
    st = {k: backtest.stats(r) for k, r in runs.items()}
    gst = {k: backtest.stats(r) for k, r in gap_runs.items()}
    diag = backtest.diagnose(runs["full_on"], cfg.fees["slippage_pct"], cfg.strategy["initial_stop_atr"])
    bq = ind["bench_close"].reindex(runs["full_on"].equity.index).dropna()
    if len(bq) > 1:
        yrs = max((bq.index[-1] - bq.index[0]).days / 365.25, 1e-9)
        diag["指数の年率_pct"] = ((bq.iloc[-1] / bq.iloc[0]) ** (1 / yrs) - 1) * 100
    cur = gap_key(cfg.strategy["entry_limit_gap_pct"])

    # ---- 図
    full_on, full_off = runs["full_on"], runs["full_off"]
    fig, ax = plt.subplots(figsize=(11, 5))
    (full_on.equity * fx).plot(ax=ax, label="Index filter ON", lw=1.6)
    (full_off.equity * fx).plot(ax=ax, label="Index filter OFF", lw=1.0, alpha=0.8)
    b = ind["bench_close"].reindex(full_on.equity.index)
    (b / b.iloc[0] * full_on.initial * fx).plot(ax=ax, label=f"{cfg.data.benchmark} buy & hold (same capital)", lw=0.8,
                                                color="gray", alpha=0.7)
    ax.set_ylabel("Equity (JPY, fixed FX)")
    ax.set_title("Equity curve")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "equity.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 3.5))
    for r, lab in ((full_on, "ON"), (full_off, "OFF")):
        ((r.equity / r.equity.cummax() - 1) * 100).plot(ax=ax, label=f"Index filter {lab}", lw=1)
    ax.set_ylabel("Drawdown (%)")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "drawdown.png", dpi=120)
    plt.close(fig)

    keys = [k for k in runs if k not in ("full_on", "full_off")]
    fig, axes = plt.subplots(1, len(keys), figsize=(4 * len(keys), 3.5), squeeze=False)
    for ax, k in zip(axes[0], keys):
        (runs[k].equity * fx).plot(ax=ax, lw=1.2)
        title = {"is_on": "In-sample", "oos_on": "Out-of-sample"}.get(k, k.replace("_on", " filter ON").replace("_off", " filter OFF"))
        ax.set_title(title, fontsize=9)
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "periods.png", dpi=110)
    plt.close(fig)

    # ---- CSV
    for k, r in runs.items():
        backtest.trades_frame(r).to_csv(out / f"trades_{k}.csv", index=False)
    pd.DataFrame(full_on.decisions).to_csv(out / "decisions_full.csv", index=False)
    for k, r in gap_runs.items():
        backtest.trades_frame(r).to_csv(out / f"trades_{k}.csv", index=False)
    payload = {"created": datetime.now().isoformat(timespec="seconds"), "data": data_info, "notes": notes,
               "fx": fx, "labels": {k: r.label for k, r in runs.items()}, "stats": st,
               "gap_compare": {k: {"label": r.label, "current": k == cur, "stats": gst[k]} for k, r in gap_runs.items()},
               "diagnose": diag, "variants": variants or {}}
    (out / "stats.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ---- Markdown
    L = []
    L.append("# バックテスト結果レポート（米国株スイング・20日高値ブレイク）\n")
    L.append(f"作成: {datetime.now():%Y-%m-%d %H:%M}　データ: {data_info.get('source')}　"
             f"銘柄数: {data_info.get('symbols')}　期間: {data_info.get('range')}\n")
    if notes:
        L.append("## ⚠ この結果を読む前に\n")
        L += [f"- {n}" for n in notes]
        L.append("")
    L.append("## 主要な評価指標\n")
    cols = [k for k in runs]
    names = {k: runs[k].label for k in cols}
    rows = [("総損益 (円)", lambda s: _fmt(s["総損益_usd"] * fx, 0)), ("総損益 (%)", lambda s: _fmt(s["総損益_pct"])),
            ("年率リターン (%)", lambda s: _fmt(s["年率リターン_pct"])), ("最大ドローダウン (%)", lambda s: _fmt(s["最大ドローダウン_pct"])),
            ("取引回数", lambda s: _fmt(s["取引回数"])), ("勝率 (%)", lambda s: _fmt(s["勝率_pct"])),
            ("損益レシオ", lambda s: _fmt(s["損益レシオ"], 2)), ("プロフィットファクター", lambda s: _fmt(s["プロフィットファクター"], 2)),
            ("平均保有日数", lambda s: _fmt(s["平均保有日数"])), ("現金比率 平均 (%)", lambda s: _fmt(s["現金比率_平均_pct"])),
            ("ノーポジション日 (%)", lambda s: _fmt(s["ノーポジション日数_pct"]))]
    L.append("| 指標 | " + " | ".join(names[k] for k in cols) + " |")
    L.append("|---|" + "---|" * len(cols))
    for name, fn in rows:
        L.append(f"| {name} | " + " | ".join(fn(st[k]) if st[k] else "—" for k in cols) + " |")
    L.append("\n期間: " + "、".join(f"{names[k]} = {st[k].get('期間')}" for k in cols) + "\n")
    L.append("![損益曲線](equity.png)\n\n![ドローダウン](drawdown.png)\n\n![期間別](periods.png)\n")

    L.append("## 年別損益（全期間・円換算）\n")
    yr_on, yr_off = st["full_on"]["年別損益_usd"], st["full_off"]["年別損益_usd"]
    L.append("| 年 | 指数フィルターあり | 指数フィルターなし |")
    L.append("|---|---:|---:|")
    for y in sorted(set(yr_on) | set(yr_off)):
        L.append(f"| {y} | {_fmt(yr_on.get(y, 0) * fx, 0)} | {_fmt(yr_off.get(y, 0) * fx, 0)} |")
    L.append("")
    if variants:
        L.append("## 改善案の比較（指数フィルターあり）\n")
        L.append("検証期間で選び、検証外期間（直近）でも勝てているかを見る。検証外だけ良い案は偶然の可能性が高い。\n")
        L.append("| 案 | 検証: 年率 (%) | 検証: 最大DD (%) | 検証: 平均R | 検証外: 年率 (%) | 検証外: 最大DD (%) | 検証外: 平均R | 取引 (検証/外) |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for v in variants.values():
            a, b = v["is"]["stats"], v["oos"]["stats"]
            da, db = v["is"]["diag"], v["oos"]["diag"]
            L.append(f"| {v['name']} | {_fmt(a.get('年率リターン_pct'))} | {_fmt(a.get('最大ドローダウン_pct'))} | "
                     f"{_fmt(da.get('平均R'), 2)} | {_fmt(b.get('年率リターン_pct'))} | {_fmt(b.get('最大ドローダウン_pct'))} | "
                     f"{_fmt(db.get('平均R'), 2)} | {a.get('取引回数', 0)}/{b.get('取引回数', 0)} |")
        L.append("")
    if diag:
        L.append("## どこで負けているか（全期間・指数フィルターあり）\n")
        L.append(f"- 損益合計 {_fmt(diag['損益合計_usd'] * fx, 0)} 円 = コスト前 {_fmt(diag['コスト前の損益_usd'] * fx, 0)} 円 "
                 f"− 手数料 {_fmt(diag['手数料合計_usd'] * fx, 0)} 円 − スリッページ {_fmt(diag['スリッページ合計_usd'] * fx, 0)} 円"
                 f"（1 取引あたりのコスト {_fmt(diag['コスト_1取引あたり_usd'] * fx, 0)} 円）")
        L.append(f"- 平均 R {_fmt(diag['平均R'], 2)}（勝ち {_fmt(diag['勝ちの平均R'], 2)} / 負け {_fmt(diag['負けの平均R'], 2)}）・"
                 f"3R 以上の大勝ち {diag['大勝ち(3R以上)_回数']} 回・最大連敗 {diag['最大連敗']} 回・"
                 f"買った日に損切り {_fmt(diag['買った日に損切り_割合_pct'])}%")
        if diag.get("指数の年率_pct") is not None:
            L.append(f"- 同じ期間の {cfg.data.benchmark} の年率 {_fmt(diag['指数の年率_pct'])}%（買って持っているだけの場合）")
        L.append("\n| 手仕舞い理由 | 回数 | 損益合計 (円) | 1 回の平均 (円) |\n|---|---:|---:|---:|")
        for k, d in sorted(diag["手仕舞い理由別"].items(), key=lambda kv: kv[1]["損益合計_usd"]):
            L.append(f"| {k} | {d['回数']} | {_fmt(d['損益合計_usd'] * fx, 0)} | {_fmt(d['平均_usd'] * fx, 0)} |")
        L.append("")
    if gap_runs:
        L.append("## 寄り付きの買い方の比較（全期間・指数フィルターあり）\n")
        L.append("「上限 +N%」= 前日終値 × (1 + N%) までなら寄り付きで買い、それより高く始まったら見送る。"
                 f"今の設定: **{gap_runs[cur].label if cur in gap_runs else cur}**\n")
        L.append("| 買い方 | 総損益 (円) | 年率 (%) | 最大DD (%) | 取引回数 | 勝率 (%) | 損益レシオ | 窓開けで見送り |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for k, r in gap_runs.items():
            s = gst[k]
            skip = sum(v for kk, v in s.get("見送り", {}).items() if "上限" in kk)
            L.append(f"| {r.label}{' ←今の設定' if k == cur else ''} | {_fmt(s['総損益_usd'] * fx, 0)} | {_fmt(s['年率リターン_pct'])} | "
                     f"{_fmt(s['最大ドローダウン_pct'])} | {s['取引回数']} | {_fmt(s['勝率_pct'])} | {_fmt(s['損益レシオ'], 2)} | {skip} |")
        L.append("")
    L.append("## 手仕舞い理由・見送り（全期間・フィルターあり）\n")
    L.append("- 手仕舞い: " + "、".join(f"{k} {v} 回" for k, v in st["full_on"]["手仕舞い理由"].items()))
    L.append("- 見送り: " + ("、".join(f"{k} {v} 回" for k, v in st["full_on"]["見送り"].items()) or "なし"))
    L.append("\n## 前提\n")
    L.append(f"- 資金 {cfg.account['capital_jpy']:,} 円 = {cfg.account['capital_jpy'] / fx:,.0f} USD（{fx} 円/USD 固定。為替変動は含まない）")
    L.append(f"- 手数料 片道 {cfg.fees['commission_pct']}%（上限 {cfg.fees['commission_max_usd']} USD）、"
             f"スリッページ 片道 {cfg.fees['slippage_pct']}%、両替コスト {cfg.account['fx_cost_pct']}%")
    L.append(f"- 最大 {cfg.strategy['max_positions']} 銘柄、1 トレードのリスク {cfg.risk['risk_per_trade_pct']}%、"
             f"1 銘柄上限 {cfg.risk['max_position_pct']}%、整数株、T+{cfg.account['settlement_days']} 受渡、"
             f"週次損失上限 {cfg.risk['weekly_loss_limit_pct']}%")
    L.append("- 判定は日足の終値、執行は翌営業日の寄り付き。損切りは口座側の逆指値として日中に約定"
             "（寄り付きで下回っていれば寄り付き値）")
    L.append("- 各期間は資金 20 万円から独立に開始。パラメータ最適化はしていない（config.yaml の固定値）")
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")
    log.info("レポートを保存: %s", out / "report.md")
    return out
