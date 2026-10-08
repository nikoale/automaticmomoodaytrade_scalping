"""コマンド:  python -m swing <command>

  fetch all        銘柄リスト → 日足 (10 年+) → 時価総額・決算日 をまとめて取得 (初回は時間がかかる)
  fetch universe   銘柄リストだけ
  fetch prices     日足だけ (差分更新)
  screen           週次スクリーナー (データ更新 → watchlist_YYYYMMDD.json)
  backtest         バックテスト一式 → reports/<日時>/report.md
  backtest --synthetic   擬似データでレポート作成の流れだけ確認 (成績に意味はない)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from . import config as config_mod


def setup_logging(cfg, name: str, level: str = "INFO") -> None:
    log_dir = config_mod.ROOT / cfg["logging"]["dir"] if "logging" in cfg else config_mod.ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(log_dir / f"{datetime.now():%Y%m%d}_{name}.log", encoding="utf-8")],
    )


def cmd_fetch(cfg, what: str, limit: int | None) -> None:
    from . import data
    if what in ("universe", "all"):
        data.update_universe(cfg)
    if what == "universe":
        return
    syms = list(data.load_universe(cfg)["symbol"])[: limit or None]
    if what in ("prices", "all"):
        data.update_prices(cfg, syms + [cfg.data.benchmark])
    if what in ("meta", "earnings", "all"):
        # 時価総額・決算日は、株価・出来高の条件を一度でも満たした銘柄だけ取る (呼び出し回数を減らす)
        panel = data.load_panel(cfg, syms)
        cand = list(panel["close"].columns)
        logging.getLogger("swing").info("時価総額・決算日の取得対象: %d 銘柄", len(cand))
        if what in ("meta", "all"):
            data.update_meta(cfg, cand)
        if what in ("earnings", "all"):
            data.update_earnings(cfg, cand)


def cmd_backtest(cfg, synthetic: bool) -> None:
    from . import data, report
    from .indicators import compute_all
    log = logging.getLogger("swing")
    notes = []
    if synthetic:
        cfg["screener"]["earnings_unknown_policy"] = "keep"
        panel = data.synthetic_panel(n_days=3000, n_syms=300, seed=7, start="2012-01-02")
        cfg["backtest"]["start"] = "2013-01-02"
        earnings, shares = {}, None
        info = {"source": "擬似データ (ランダム生成)", "symbols": 300, "range": "2012〜"}
        notes.append("**これは擬似データ（ランダム生成）での動作確認です。成績に意味はありません。**")
    else:
        uni = data.load_universe(cfg)
        panel = data.load_panel(cfg, list(uni["symbol"]), start=cfg.data.history_start)
        syms = list(panel["close"].columns)
        earnings = {s: data.read_earnings(cfg, s) for s in syms}
        have_e = sum(1 for v in earnings.values() if v)
        meta = data.load_meta(cfg)
        shares = None
        if cfg.backtest["market_cap_proxy"] == "current_shares":
            shares = {s: float(meta.at[s, "shares"]) for s in syms if s in meta.index and meta.at[s, "shares"] == meta.at[s, "shares"]}
        idx = panel["close"].index
        info = {"source": f"{cfg.data.price_source} (分割・配当調整済み)", "symbols": len(syms),
                "range": f"{idx[0].date()} 〜 {idx[-1].date()}"}
        notes += [
            "**生存者バイアス**: 銘柄リストは現在上場している銘柄のみ（上場廃止・買収された銘柄を含まない）。"
            "成績は実際より良く出る方向に偏る。",
            f"**決算日データ**: {have_e}/{len(syms)} 銘柄で取得。決算日が不明な銘柄は "
            f"`earnings_unknown_policy={cfg.screener['earnings_unknown_policy']}` で扱った。",
            ("**時価総額**: 過去の時価総額は無料で取れないため「現在の発行済株数 × その日の株価」で近似"
             f"（{len(shares or {})}/{len(syms)} 銘柄で株数を取得、取れない銘柄は時価総額条件で除外）。")
            if shares is not None else "**時価総額**: バックテストでは時価総額の条件を使っていない (market_cap_proxy=none)。",
        ]
    ind = compute_all(panel, cfg)
    runs = report.run_suite(cfg, ind, earnings, shares)
    out = report.write(cfg, runs, ind, notes, info)
    log.info("完了: %s", out / "report.md")
    print(out / "report.md")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="swing", description="米国株スイング自動売買ボット")
    p.add_argument("-c", "--config", default=None)
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("what", choices=["all", "universe", "prices", "meta", "earnings"])
    f.add_argument("--limit", type=int, help="動作確認用: 先頭 N 銘柄だけ")
    s = sub.add_parser("screen")
    s.add_argument("--no-update", action="store_true", help="データを更新せずキャッシュで実行")
    b = sub.add_parser("backtest")
    b.add_argument("--synthetic", action="store_true")
    a = p.parse_args(argv)
    cfg = config_mod.load(a.config)
    setup_logging(cfg, a.cmd, a.log_level)
    if a.cmd == "fetch":
        cmd_fetch(cfg, a.what, a.limit)
    elif a.cmd == "screen":
        from .screener import run_weekly
        print(run_weekly(cfg, update=not a.no_update))
    elif a.cmd == "backtest":
        cmd_backtest(cfg, a.synthetic)


if __name__ == "__main__":
    main()
