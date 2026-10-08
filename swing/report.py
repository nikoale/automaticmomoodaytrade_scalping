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
    return runs


def write(cfg, runs: dict, ind: dict, notes: list[str], data_info: dict) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out = cfg.report_dir() / datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    fx = cfg.account["fx_rate_jpy_per_usd"]
    st = {k: backtest.stats(r) for k, r in runs.items()}

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
    payload = {"created": datetime.now().isoformat(timespec="seconds"), "data": data_info, "notes": notes,
               "fx": fx, "labels": {k: r.label for k, r in runs.items()}, "stats": st}
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
