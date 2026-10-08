"""ブラウザで操作する GUI (ローカル専用の小さな Web サーバ)。

python -m scalper gui で起動 → http://127.0.0.1:8765 が開く。
外部ライブラリは使わず標準ライブラリのみ。127.0.0.1 にのみ待ち受け、起動ごとのトークンで
他の Web サイトからの操作を防ぐ。
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import webbrowser
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..backtest import run_backtest
from ..config import Config, load_config
from ..data import generate_sample
from ..live import LiveRunner
from ..strategies import STRATEGIES

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).resolve().parent / "static"
SETTINGS_FILE = ROOT / "config" / "gui_settings.json"

PRESETS = {
    "us": {"label": "通常取引のみ（日本時間 22:30〜翌5:00 ※冬は+1時間）", "file": "config/config.us.example.yaml"},
    "us_ext": {"label": "時間外も含む（プレ・アフター・オーバーナイト）", "file": "config/config.us.ext.example.yaml"},
}

STRATEGY_INFO = {
    "ema_vwap": "順張り：短期EMAが長期EMAを上抜け＋VWAPより上＋出来高増で買い",
    "orb": "寄り付きブレイク：最初のN分の高値/安値を抜けたら追いかける",
    "vwap_reversion": "逆張り：VWAPから大きく離れたらVWAPへ戻る方向に入る",
}


class LogBuffer(logging.Handler):
    """画面に出すログを溜めておく。"""

    def __init__(self, size: int = 1000):
        super().__init__()
        self.lines: deque[tuple[int, str, str]] = deque(maxlen=size)
        self.seq = 0
        # ※ logging.Handler 自身が self.lock を使うので別名にする (上書きするとデッドロック)
        self._buf_lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:  # pragma: no cover
            return
        with self._buf_lock:
            self.seq += 1
            self.lines.append((self.seq, record.levelname, msg))

    def since(self, n: int) -> list[dict]:
        with self._buf_lock:
            return [{"id": i, "level": lv, "msg": m} for i, lv, m in self.lines if i > n]


def build_config(s: dict) -> Config:
    """画面の設定値 → Config。"""
    preset = PRESETS.get(s.get("preset") or "us", PRESETS["us"])
    cfg = load_config(ROOT / preset["file"], {"mode": "backtest"})
    symbols = [x.strip().upper() for x in str(s.get("symbols", "")).replace("、", ",").split(",") if x.strip()]
    if symbols:
        cfg.symbols = symbols
    if s.get("strategy") in STRATEGIES and s["strategy"] != cfg.strategy.name:
        cfg.strategy.name, cfg.strategy.params = s["strategy"], {}
    r = cfg.risk
    for key, attr, conv in (("account_size", "account_size", float), ("max_daily_loss", "max_daily_loss", float),
                            ("max_position_value", "max_position_value", float)):
        if s.get(key) not in (None, ""):
            setattr(r, attr, conv(s[key]))
    if s.get("risk_pct") not in (None, ""):
        r.risk_per_trade = float(s["risk_pct"]) / 100.0
    if "auto" in s:
        cfg.auto_symbols.enabled = bool(s["auto"])
    if s.get("auto_count") not in (None, ""):
        cfg.auto_symbols.count = int(s["auto_count"])
    if s.get("host"):
        cfg.moomoo.host = str(s["host"])
    if s.get("port"):
        cfg.moomoo.port = int(s["port"])
    cfg.log_dir = str(ROOT / "logs")
    mode = s.get("mode") or "paper"
    if mode not in ("paper", "simulate"):
        raise ValueError("GUI では paper / simulate のみ使えます (実口座はコマンドの --confirm-live で)")
    if mode == "simulate" and cfg.session.us_session.upper() != "RTH":
        raise ValueError("時間外取引は paper のみ対応です (moomoo 模擬口座での時間外注文は未検証)")
    cfg.mode = mode
    cfg.validate()
    return cfg


def preset_defaults() -> dict:
    out = {}
    for key, p in PRESETS.items():
        cfg = load_config(ROOT / p["file"], {"mode": "backtest"})
        out[key] = {
            "label": p["label"], "symbols": ", ".join(cfg.symbols), "strategy": cfg.strategy.name,
            "account_size": cfg.risk.account_size, "risk_pct": round(cfg.risk.risk_per_trade * 100, 3),
            "max_daily_loss": cfg.risk.max_daily_loss, "max_position_value": cfg.risk.max_position_value,
            "currency": "USD", "auto_count": cfg.auto_symbols.count,
            # 時間外の注文は moomoo 模擬口座で未検証なので paper のみ
            "simulate_ok": cfg.session.us_session.upper() == "RTH",
        }
    return out


class App:
    def __init__(self):
        self.token = secrets.token_urlsafe(16)
        self.logs = LogBuffer()
        self.runner: LiveRunner | None = None
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        root = logging.getLogger()
        root.addHandler(self.logs)
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)

    # ---- settings
    def load_settings(self) -> dict:
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_settings(self, s: dict) -> None:
        try:
            SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---- actions
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, s: dict) -> dict:
        with self.lock:
            if self.running():
                return {"ok": False, "error": "すでに稼働中です"}
            cfg = build_config(s)
            self.save_settings(s)
            self.runner = LiveRunner(cfg)
            self.thread = threading.Thread(target=self.runner.run, name="runner", daemon=True)
            self.thread.start()
            return {"ok": True}

    def stop(self) -> dict:
        with self.lock:
            if not self.running():
                return {"ok": True}
            self.runner.stop()
            self.thread.join(timeout=20)
            return {"ok": not self.running()}

    def command(self, cmd: str) -> dict:
        if not self.running() or cmd not in ("flatten", "pause", "resume"):
            return {"ok": False, "error": "稼働中ではありません"}
        self.runner.request(cmd)
        return {"ok": True}

    def state(self, since: int) -> dict:
        snap = self.runner.snapshot() if self.runner else {"phase": "idle", "symbols": []}
        snap["running"] = self.running()
        snap["logs"] = self.logs.since(since)
        return snap

    def check(self, s: dict) -> dict:
        from ..tools import run_check
        lines: list[str] = []
        try:
            ok = run_check(build_config(s), lines.append)
        except Exception as e:  # 接続失敗など
            lines.append(f"❌ {e}")
            ok = False
        return {"ok": ok, "lines": lines}

    def symbols(self, s: dict, query: str) -> dict:
        from ..tools import search_symbols
        cfg = build_config(s)
        rows, total = search_symbols(cfg, query, 100)
        return {"ok": True, "rows": rows, "total": total}

    def screen(self, s: dict) -> dict:
        from ..tools import screen_preview
        cfg = build_config(s)
        rows = screen_preview(cfg)
        for r in rows[:60]:
            r["chosen"] = False
        for r in [r for r in rows if r["excluded"] is None][: cfg.auto_symbols.count]:
            r["chosen"] = True
        return {"ok": True, "rows": rows[:60], "total": len(rows), "count": cfg.auto_symbols.count}

    def backtest(self, s: dict, source: str, days: int) -> dict:
        cfg = build_config(s)
        cfg.mode = "backtest"
        data = {}
        if source == "moomoo":
            from ..tools import fetch_history
            end = datetime.now()
            start = end - timedelta(days=days)
            for code in cfg.symbols:
                data[code] = fetch_history(cfg, code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                                           cfg.bar_minutes)
        else:
            for i, code in enumerate(cfg.symbols):
                data[code] = generate_sample(days=days, seed=42 + i)
        res = run_backtest(cfg, data)
        st = res.stats()
        if st["profit_factor"] == float("inf"):
            st["profit_factor"] = None
        return {
            "ok": True, "stats": st, "bars": sum(len(v) for v in data.values()),
            "equity": [[str(t), eq] for t, eq in res.equity_curve],
            "trades": [{"code": t.code, "direction": t.direction, "qty": t.qty, "entry_time": str(t.entry_time),
                        "entry": t.entry_price, "exit_time": str(t.exit_time), "exit": t.exit_price,
                        "pnl": t.pnl, "reason": t.reason} for t in res.trades[-300:]],
        }


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # アクセスログは出さない
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Token", ""), app.token)

        def do_GET(self):
            path, _, qs = self.path.partition("?")
            if path in ("/", "/index.html"):
                html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", app.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                return self._json({"ok": False, "error": "not found"}, 404)
            if not self._authorized():
                return self._json({"ok": False, "error": "unauthorized"}, 403)
            if path == "/api/meta":
                return self._json({"presets": preset_defaults(), "strategies": STRATEGY_INFO,
                                   "settings": app.load_settings()})
            if path == "/api/state":
                since = 0
                for part in qs.split("&"):
                    if part.startswith("since="):
                        since = int(part[6:] or 0)
                return self._json(app.state(since))
            self._json({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            if not self._authorized():
                return self._json({"ok": False, "error": "unauthorized"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"ok": False, "error": "bad json"}, 400)
            s = body.get("settings") or {}
            try:
                if self.path == "/api/start":
                    return self._json(app.start(s))
                if self.path == "/api/stop":
                    return self._json(app.stop())
                if self.path == "/api/command":
                    return self._json(app.command(body.get("cmd", "")))
                if self.path == "/api/check":
                    return self._json(app.check(s))
                if self.path == "/api/symbols":
                    return self._json(app.symbols(s, body.get("query", "")))
                if self.path == "/api/screen":
                    return self._json(app.screen(s))
                if self.path == "/api/backtest":
                    return self._json(app.backtest(s, body.get("source", "sample"), int(body.get("days", 20))))
                if self.path == "/api/settings":
                    app.save_settings(s)
                    return self._json({"ok": True})
            except Exception as e:
                log.exception("api error")
                return self._json({"ok": False, "error": str(e)})
            self._json({"ok": False, "error": "not found"}, 404)

    return Handler


def serve(port: int = 8765, open_browser: bool = True) -> None:
    app = App()
    httpd = None
    for p in range(port, port + 10):   # 使用中なら次のポート
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), make_handler(app))
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("GUI 用のポートを確保できませんでした")
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  GUI を起動しました → {url}\n  (このウィンドウを閉じるとボットも止まります)\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("終了します...")
        app.stop()
        httpd.server_close()
