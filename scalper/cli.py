"""コマンドライン: python -m scalper <command>"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import load_config


def _setup_logging(level: str, log_dir: str | None = None, name: str = "scalper") -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(Path(log_dir) / f"{name}_{datetime.now():%Y%m%d}.log",
                                            encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper()), handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def cmd_sample(a) -> None:
    from .data import generate_sample, save_csv
    bars = generate_sample(days=a.days, start_price=a.price, market=a.market, seed=a.seed)
    save_csv(bars, a.out)
    print(f"wrote {len(bars)} bars -> {a.out}  (※ランダム生成の擬似データです)")


def cmd_backtest(a) -> None:
    from .backtest import format_stats, run_backtest
    from .data import load_csv
    overrides = {"mode": "backtest"}
    cfg = load_config(a.config, overrides)
    if a.strategy and a.strategy != cfg.strategy.name:
        # 戦略を切り替えたら config の params (別戦略用) は使わずデフォルト値で
        cfg.strategy.name, cfg.strategy.params = a.strategy, {}
    _setup_logging(a.log_level or "WARNING")
    data = {}
    for item in a.csv:
        # "JP.7203=data/7203.csv" 形式、またはパスのみ (銘柄は config の先頭)
        code, _, path = item.rpartition("=")
        data[code or cfg.symbols[0]] = load_csv(path)
    res = run_backtest(cfg, data)
    print(f"strategy={cfg.strategy.name} params={cfg.strategy.params} bar={cfg.bar_minutes}m")
    print(format_stats(res.stats(), " 円" if cfg.market == "JP" else " USD"))
    if a.trades_out:
        res.save_trades(a.trades_out)
        print(f"trades -> {a.trades_out}")


def cmd_fetch(a) -> None:
    """moomoo から過去の分足を取得して CSV に保存 (OpenD 起動が必要)。"""
    from .data import save_csv
    from .tools import fetch_history
    cfg = load_config(a.config)
    _setup_logging(a.log_level or "INFO")
    bars = fetch_history(cfg, a.symbol, a.start, a.end, a.bar_minutes)
    save_csv(bars, a.out)
    print(f"wrote {len(bars)} bars -> {a.out}")


def cmd_check(a) -> None:
    from .tools import run_check
    ok = run_check(load_config(a.config), print)
    if not ok:
        raise SystemExit(1)


def cmd_symbols(a) -> None:
    from .tools import search_symbols
    cfg = load_config(a.config)
    rows, total = search_symbols(cfg, a.market, a.grep, a.limit)
    for r in rows:
        print(f"{r['code']:20} {r['name']}  (lot={r['lot_size']})")
    print(f"-- {total} 件 (表示 {len(rows)} 件)")


def cmd_gui(a) -> None:
    from .gui.server import serve
    _setup_logging(a.log_level or "INFO", str(Path(__file__).resolve().parents[1] / "logs"), "gui")
    serve(port=a.port, open_browser=not a.no_browser)


def cmd_run(a) -> None:
    from .live import LiveRunner
    overrides = {"mode": a.mode} if a.mode else None
    cfg = load_config(a.config, overrides)
    if cfg.mode == "backtest":
        raise SystemExit("run は paper / simulate / live モードで使います (config の mode か --mode を指定)")
    if cfg.mode == "live" and not a.confirm_live:
        raise SystemExit("実口座で発注するには --confirm-live を付けてください。まず paper / simulate で十分に検証を。")
    _setup_logging(a.log_level or "INFO", cfg.log_dir, f"run_{cfg.mode}")
    LiveRunner(cfg).run()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="scalper", description="moomoo証券 スキャルピング / デイトレード bot")
    p.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING (backtest の既定は WARNING)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="動作確認用の擬似 1 分足 CSV を生成")
    s.add_argument("--days", type=int, default=20)
    s.add_argument("--price", type=float, default=3000.0)
    s.add_argument("--market", default="JP")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--out", default="data/sample_1m.csv")
    s.set_defaults(func=cmd_sample)

    b = sub.add_parser("backtest", help="CSV の足でバックテスト")
    b.add_argument("-c", "--config", default=None)
    b.add_argument("--csv", nargs="+", required=True, help="CSV パス、または CODE=PATH (複数可)")
    b.add_argument("--strategy", choices=["ema_vwap", "orb", "vwap_reversion"])
    b.add_argument("--trades-out")
    b.set_defaults(func=cmd_backtest)

    f = sub.add_parser("fetch", help="moomoo から過去分足を取得して CSV 保存")
    f.add_argument("-c", "--config", default=None)
    f.add_argument("--symbol", required=True, help="例: JP.7203 / US.AAPL")
    f.add_argument("--start", required=True, help="YYYY-MM-DD")
    f.add_argument("--end", required=True, help="YYYY-MM-DD")
    f.add_argument("--bar-minutes", type=int, default=1, choices=[1, 3, 5, 15])
    f.add_argument("--out", required=True)
    f.set_defaults(func=cmd_fetch)

    c = sub.add_parser("check", help="OpenD 接続・相場権限・現在値・板を確認 (市場時間外でも可)")
    c.add_argument("-c", "--config", required=True)
    c.set_defaults(func=cmd_check)

    y = sub.add_parser("symbols", help="銘柄コード一覧 (例: --market CC --grep BTC)")
    y.add_argument("-c", "--config", required=True)
    y.add_argument("--market", help="CC / US / JP (省略時は config の trd_market)")
    y.add_argument("--grep", help="コード・名前の部分一致")
    y.add_argument("--limit", type=int, default=50)
    y.set_defaults(func=cmd_symbols)

    g = sub.add_parser("gui", help="ブラウザで操作する画面を起動")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--no-browser", action="store_true")
    g.set_defaults(func=cmd_gui)

    r = sub.add_parser("run", help="リアルタイム売買 (paper / simulate / live)")
    r.add_argument("-c", "--config", required=True)
    r.add_argument("--mode", choices=["paper", "simulate", "live"])
    r.add_argument("--confirm-live", action="store_true", help="実口座での発注を許可する")
    r.set_defaults(func=cmd_run)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
