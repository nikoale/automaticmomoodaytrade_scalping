"""コマンド:  python -m swing <command>

  fetch all        銘柄リスト → 日足 (10 年+) → 時価総額・決算日 をまとめて取得 (初回は時間がかかる)
  fetch universe   銘柄リストだけ
  fetch prices     日足だけ (差分更新)
  screen           週次スクリーナー (→ data/watchlists/watchlist_YYYYMMDD.json)。--source moomoo / free
  quota            moomoo の過去 K 線の取得枠を表示
  backtest         バックテスト一式 → reports/<日時>/report.md
  backtest --synthetic   擬似データでレポート作成の流れだけ確認 (成績に意味はない)
  trade close      引け後の処理 (照合 → 逆指値 → 損切り更新・手仕舞い・新規エントリーの判定 → 予約)
  trade open       寄り付き後の処理 (未発注の注文を出す → 約定を待つ → 逆指値)
  trade status     保存されている建玉・注文・停止状態を表示 (接続しない)
  trade resume     発注停止を解除する (原因を確かめてから)
  trade reset      模擬口座の記録をリセット (ボットの有効な注文を取り消して最初から)
  run              フェーズ 4: 常駐して自動実行 (引け後の処理・寄り付き後の処理・週次スクリーナー)。VPS 用
  trade check      発注機能の確認 (模擬口座だけ。指値・取消・成行の予約・逆指値・訂正)
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from . import config as config_mod


class DailyFileHandler(logging.Handler):
    """日付ごとのログファイル (logs/YYYYMMDD_<name>.log)。常駐していても日付が変われば新しいファイルに書く。"""

    def __init__(self, log_dir, name: str):
        super().__init__()
        self.dir, self.name = log_dir, name
        self.day, self.fh = None, None

    def emit(self, record):
        day = datetime.now().strftime("%Y%m%d")
        try:
            if day != self.day:
                if self.fh:
                    self.fh.close()
                self.dir.mkdir(parents=True, exist_ok=True)
                self.fh = open(self.dir / f"{day}_{self.name}.log", "a", encoding="utf-8")
                self.day = day
            self.fh.write(self.format(record) + "\n")
            self.fh.flush()
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def close(self):
        if self.fh:
            self.fh.close()
        super().close()


def setup_logging(cfg, name: str, level: str = "INFO") -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    handlers = [logging.StreamHandler(sys.stdout), DailyFileHandler(cfg.log_dir(), name)]
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=getattr(logging, level), handlers=handlers)


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
    return out


def confirm_real() -> bool:
    """本番口座の 2 つ目のロック: 起動した人が決まった文を入力する。"""
    from .broker import REAL_CONFIRM_PHRASE
    if not sys.stdin.isatty():
        print("本番口座の確認は、画面 (ターミナル) から人が入力する必要があります")
        return False
    print("\n!!! 本番口座 (REAL) で実際のお金の注文を出します !!!")
    ans = input(f"よければ「{REAL_CONFIRM_PHRASE}」と入力してください: ").strip()
    return ans == REAL_CONFIRM_PHRASE


def cmd_trade(cfg, what: str, yes: bool = False) -> None:
    import json as _json

    from . import executor
    env = str(cfg["moomoo"]["trd_env"]).upper()
    if what == "status":
        print(_json.dumps(executor.read_summary(cfg, env), ensure_ascii=False, indent=1, default=str))
        return
    if what == "resume":
        st = executor.load_state(cfg, env)
        if not st["halt"]["on"]:
            print("停止していません")
            return
        print(f"停止の理由: {st['halt']['reason']}")
        if not yes and input("原因を確かめましたか？ 解除するなら yes: ").strip() != "yes":
            print("解除しませんでした")
            return
        st["halt"] = {"on": False, "reason": "", "time": None}
        executor.save_state(cfg, env, st)
        logging.getLogger("swing").warning("[停止解除] コマンドから解除しました")
        return
    with executor.Session(cfg, confirm=confirm_real) as ex:
        if what == "close":
            s = ex.run_close()
        elif what == "open":
            s = ex.run_open()
        elif what == "reset":
            s = ex.reset()
        else:
            s = executor.capability_check(cfg, ex.broker)
    print(_json.dumps(s, ensure_ascii=False, indent=1, default=str))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="swing", description="米国株スイング自動売買ボット")
    p.add_argument("-c", "--config", default=None)
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("what", choices=["all", "universe", "prices", "meta", "earnings"])
    f.add_argument("--limit", type=int, help="動作確認用: 先頭 N 銘柄だけ")
    s = sub.add_parser("screen")
    s.add_argument("--no-update", action="store_true", help="(free のみ) データを更新せずキャッシュで実行")
    s.add_argument("--source", choices=["moomoo", "free"], help="config の data.screener_source を上書き")
    sub.add_parser("quota", help="moomoo の過去 K 線の取得枠 (使用済み / 残り) を表示")
    g = sub.add_parser("gui", help="ブラウザで操作する画面")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--no-browser", action="store_true")
    b = sub.add_parser("backtest")
    b.add_argument("--synthetic", action="store_true")
    sub.add_parser("run", help="フェーズ 4: 常駐して自動実行")
    t = sub.add_parser("trade", help="フェーズ 3: 発注 (既定は模擬口座)")
    t.add_argument("what", choices=["close", "open", "status", "resume", "check", "reset"])
    t.add_argument("--yes", action="store_true", help="resume の確認を省略")
    a = p.parse_args(argv)
    cfg = config_mod.load(a.config)
    setup_logging(cfg, a.cmd, a.log_level)
    if a.cmd == "fetch":
        cmd_fetch(cfg, a.what, a.limit)
    elif a.cmd == "screen":
        from .screener import run_weekly
        if a.source:
            cfg["data"]["screener_source"] = a.source
        print(run_weekly(cfg, update=not a.no_update))
    elif a.cmd == "quota":
        from .moomoo_data import MoomooData
        with MoomooData(cfg) as m:
            used, remain = m.quota()
        print(f"過去 K 線の取得枠: 使用済み {used} / 残り {remain}")
    elif a.cmd == "backtest":
        print(cmd_backtest(cfg, a.synthetic) / "report.md")
    elif a.cmd == "run":
        from .runner import run_forever
        run_forever(lambda: config_mod.load(a.config), confirm=confirm_real)
    elif a.cmd == "trade":
        cmd_trade(cfg, a.what, a.yes)
    elif a.cmd == "gui":
        from .gui import serve
        serve(port=a.port, open_browser=not a.no_browser, config_path=a.config)


if __name__ == "__main__":
    main()
