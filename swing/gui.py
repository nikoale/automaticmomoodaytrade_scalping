"""ブラウザで操作する画面 (ローカル専用の小さな Web サーバ。標準ライブラリのみ)。

python -m swing gui → http://127.0.0.1:8765 が開く。
- 127.0.0.1 にのみ待ち受け、起動ごとのトークンで他の Web サイトからの操作を防ぐ
- 時間のかかる処理 (データ取得・スクリーナー・バックテスト) は裏で 1 つずつ実行し、ログを画面に流す
- 発注はしない (フェーズ 3 は未実装)
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import traceback
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import calendar_us
from . import config as config_mod

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
SETTINGS_KEYS = {  # 画面で変えられる設定 → config.yaml のどこを上書きするか
    "capital_jpy": ("account", "capital_jpy", int),
    "fx_rate_jpy_per_usd": ("account", "fx_rate_jpy_per_usd", float),
    "screener_source": ("data", "screener_source", str),
    "capital_source": ("account", "capital_source", str),
    "account_env": (None, None, str),            # 口座タブで見る口座 (REAL / SIMULATE)。画面だけの設定
    "host": ("moomoo", "host", str),
    "port": ("moomoo", "port", int),
}


class LogBuffer(logging.Handler):
    def __init__(self, size: int = 2000):
        super().__init__()
        self.lines: deque = deque(maxlen=size)
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


class App:
    def __init__(self, config_path: str | None = None):
        self.token = secrets.token_urlsafe(16)
        self.config_path = config_path
        self.logs = LogBuffer()
        root = logging.getLogger()
        root.addHandler(self.logs)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
        self.job: dict | None = None
        self.job_lock = threading.Lock()
        self.account: dict | None = None      # 口座の残高 (メモリ上だけ。ファイルには保存しない)
        from .passwords import PasswordStore
        self.passwords = PasswordStore()

    # ---------------------------------------------------------------- 設定
    def settings_path(self) -> Path:
        return config_mod.load(self.config_path).path("gui_settings.json")

    def load_settings(self) -> dict:
        try:
            return json.loads(self.settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_settings(self, s: dict) -> dict:
        clean = {}
        for k, (_, _, conv) in SETTINGS_KEYS.items():
            if s.get(k) not in (None, ""):
                clean[k] = conv(s[k])
        if clean.get("screener_source") not in (None, "moomoo", "free"):
            raise ValueError("データ元は moomoo / free")
        if clean.get("capital_source") not in (None, "account", "config"):
            raise ValueError("資金の決め方は account / config")
        if clean.get("account_env") not in (None, "REAL", "SIMULATE"):
            raise ValueError("口座は REAL / SIMULATE")
        if "capital_jpy" in clean and clean["capital_jpy"] <= 0:
            raise ValueError("資金は正の数で")
        if clean.get("account_env") != self.load_settings().get("account_env", "REAL"):
            self.account = None             # 実口座 / 模擬口座 を切り替えたら前の数字は消す
        p = self.settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        return clean

    def cfg(self):
        over: dict = {}
        for k, v in self.load_settings().items():
            if k in SETTINGS_KEYS and SETTINGS_KEYS[k][0]:
                sec, key, _ = SETTINGS_KEYS[k]
                over.setdefault(sec, {})[key] = v
        return config_mod.load(self.config_path, over)

    # ---------------------------------------------------------------- ジョブ
    def running(self) -> bool:
        return bool(self.job and self.job["state"] == "running")

    def start_job(self, name: str) -> dict:
        jobs = {"check": self._job_check, "screen": self._job_screen, "fetch": self._job_fetch,
                "account": self._job_account, "verify_password": self._job_verify_password,
                "backtest": lambda: self._job_backtest(False), "backtest_synthetic": lambda: self._job_backtest(True)}
        if name not in jobs:
            return {"ok": False, "error": f"不明な処理: {name}"}
        with self.job_lock:
            if self.running():
                return {"ok": False, "error": f"「{JOB_LABELS[self.job['name']]}」を実行中です。終わるまで待ってください"}
            self.job = {"name": name, "label": JOB_LABELS[name], "state": "running", "started": time.time(),
                        "finished": None, "error": None, "result": None}
            job = self.job
        threading.Thread(target=self._run_job, args=(job, jobs[name]), daemon=True, name=f"job-{name}").start()
        return {"ok": True}

    def _run_job(self, job: dict, fn) -> None:
        log.info("▶ %s を開始", job["label"])
        try:
            job["result"] = fn()
            job["state"] = "done"
            log.info("✔ %s が完了", job["label"])
        except BaseException as e:  # noqa: BLE001 - 画面に表示する
            job["state"] = "error"
            job["error"] = str(e) or e.__class__.__name__
            log.error("✖ %s でエラー: %s", job["label"], job["error"])
            log.debug(traceback.format_exc())
        finally:
            job["finished"] = time.time()

    def _job_check(self) -> dict:
        from .moomoo_data import MoomooData
        cfg = self.cfg()
        log.info("OpenD %s:%s に接続します", cfg["moomoo"]["host"], cfg["moomoo"]["port"])
        with MoomooData(cfg) as m:
            ret, st = m.ctx.get_global_state()
            if ret != m.mm.RET_OK:
                raise RuntimeError(f"OpenD に接続できません: {st}")
            used, remain = m.quota()
            info = {}
            try:
                r2, info = m.ctx.get_user_info()
                info = info if r2 == m.mm.RET_OK and isinstance(info, dict) else {}
            except Exception:  # noqa: BLE001 - 権限表示は補助情報
                info = {}
        res = {"qot_logined": str(st.get("qot_logined")) in ("1", "True"),
               "trd_logined": str(st.get("trd_logined")) in ("1", "True"),
               "market_us": st.get("market_us"), "quota_used": used, "quota_remain": remain,
               "us_qot_right": info.get("us_qot_right")}
        log.info("ログイン: 相場=%s 取引=%s / 米国株の相場権限=%s / 過去K線の取得枠: 使用 %d・残り %d",
                 "OK" if res["qot_logined"] else "NG", "OK" if res["trd_logined"] else "NG",
                 res["us_qot_right"], used, remain)
        return res

    def _job_account(self) -> dict:
        from .account import AccountReader
        env = self.load_settings().get("account_env", "REAL")
        with AccountReader(self.cfg(), env) as r:
            self.account = r.snapshot()
        return {"env": env, "positions": len(self.account["positions"])}

    def _job_verify_password(self) -> dict:
        from .passwords import verify
        pw, src = self.passwords.get()
        if not pw:
            raise RuntimeError("取引パスワードが設定されていません")
        ok, msg = verify(self.cfg(), pw)
        log.info("取引パスワードの確認: %s", "OK" if ok else "NG")
        if not ok:
            raise RuntimeError(msg)
        return {"message": msg, "source": src}

    def password_action(self, action: str, pw: str | None) -> dict:
        """① この起動中だけ / ② キーチェーン の保存・削除。応答にパスワードは含めない。"""
        if action == "session":
            self.passwords.use_for_session(pw or "")
        elif action == "keychain":
            self.passwords.save_keychain(pw or "")
            self.passwords.forget_session()       # キーチェーンに入れたらメモリの方は消す
        elif action == "forget_session":
            self.passwords.forget_session()
        elif action == "delete_keychain":
            self.passwords.delete_keychain()
        else:
            raise ValueError(f"不明な操作: {action}")
        return self.passwords.status()

    def _job_screen(self) -> dict:
        from .screener import run_weekly
        path = run_weekly(self.cfg())
        return {"path": str(path)}

    def _job_fetch(self) -> dict:
        from .__main__ import cmd_fetch
        cmd_fetch(self.cfg(), "all", None)
        return self.data_status()

    def _job_backtest(self, synthetic: bool) -> dict:
        from .__main__ import cmd_backtest
        out = cmd_backtest(self.cfg(), synthetic)
        return {"dir": out.name}

    # ---------------------------------------------------------------- 表示用の情報
    def data_status(self) -> dict:
        cfg = self.cfg()
        prices = list(cfg.path("prices").glob("*.csv")) if cfg.path("prices").exists() else []
        uni = cfg.path("universe.csv")
        bench = cfg.path("prices", f"{cfg.data.benchmark}.csv")
        last = None
        if bench.exists():
            with open(bench, encoding="utf-8") as fh:
                lines = fh.read().strip().splitlines()
            last = lines[-1].split(",")[0] if len(lines) > 1 else None
        earn = cfg.path("earnings")
        return {"dir": str(cfg.path()).replace(str(Path.home()), "~"),
                "universe": max(sum(1 for _ in open(uni, encoding="utf-8")) - 1, 0) if uni.exists() else 0,
                "prices": len(prices), "earnings": len(list(earn.glob("*.csv"))) if earn.exists() else 0,
                "last_date": last}

    def latest_watchlist(self) -> dict | None:
        d = self.cfg().path("watchlists")
        files = sorted(d.glob("watchlist_*.json")) if d.exists() else []
        if not files:
            return None
        wl = json.loads(files[-1].read_text(encoding="utf-8"))
        wl["file"] = files[-1].name
        return wl

    def reports_dir(self) -> Path:
        cfg = self.cfg()
        return cfg.report_dir()

    def latest_report(self) -> dict | None:
        d = self.reports_dir()
        dirs = sorted(x for x in d.iterdir() if (x / "stats.json").exists()) if d.exists() else []
        if not dirs:
            return None
        rep = json.loads((dirs[-1] / "stats.json").read_text(encoding="utf-8"))
        if "stats" not in rep:          # 古い形式
            rep = {"stats": rep, "labels": {k: k for k in rep}, "notes": [], "data": {}, "fx": 150}
        rep["dir"] = dirs[-1].name
        rep["images"] = [f for f in ("equity.png", "drawdown.png", "periods.png") if (dirs[-1] / f).exists()]
        return rep

    def status(self, since: int) -> dict:
        job = dict(self.job) if self.job else None
        if job:
            job["elapsed"] = round((job["finished"] or time.time()) - job["started"])
        cfg = self.cfg()
        from .account import effective_capital
        acc_total = self.account["summary"]["total_assets"]["usd"] if self.account else None
        acc_fx = self.account["fx"] if self.account else None
        settings = {k: cfg[s][key] for k, (s, key, _) in SETTINGS_KEYS.items() if s}
        settings["account_env"] = self.load_settings().get("account_env", "REAL")
        return {"job": job, "logs": self.logs.since(since), "settings": settings,
                "account": self.account, "capital": effective_capital(cfg, acc_total, acc_fx),
                "password": self.passwords.status(),
                "data": self.data_status(), "watchlist": self.latest_watchlist(), "report": self.latest_report(),
                "now_jst": datetime.now(calendar_us.TOKYO).strftime("%Y-%m-%d %H:%M")}


JOB_LABELS = {"check": "OpenD 接続チェック", "account": "口座の読み込み", "verify_password": "取引パスワードの確認", "screen": "今週の監視リスト作成", "fetch": "過去データの取得",
              "backtest": "バックテスト", "backtest_synthetic": "バックテスト (擬似データ)"}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
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

        def _authorized(self, qs: str = "") -> bool:
            tok = self.headers.get("X-Token", "")
            if not tok and qs:   # 画像は <img> から読むのでクエリでも受け付ける
                for part in qs.split("&"):
                    if part.startswith("t="):
                        tok = part[2:]
            return secrets.compare_digest(tok, app.token)

        def do_GET(self):
            path, _, qs = self.path.partition("?")
            if path in ("/", "/index.html"):
                html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", app.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if not (path.startswith("/api/") or path.startswith("/reports/")):
                return self._json({"error": "not found"}, 404)
            if not self._authorized(qs):
                return self._json({"error": "unauthorized"}, 403)
            if path == "/api/status":
                since = 0
                for part in qs.split("&"):
                    if part.startswith("since="):
                        since = int(part[6:] or 0)
                return self._json(app.status(since))
            if path.startswith("/reports/"):
                return self._report_file(path[len("/reports/"):])
            self._json({"error": "not found"}, 404)

        def _report_file(self, rel: str):
            base = app.reports_dir().resolve()
            target = (base / rel).resolve()
            if base not in target.parents or not target.is_file():      # ../ などで外に出させない
                return self._json({"error": "not found"}, 404)
            ctype = {".png": "image/png", ".md": "text/markdown; charset=utf-8", ".csv": "text/csv; charset=utf-8",
                     ".json": "application/json"}.get(target.suffix, "application/octet-stream")
            self._send(200, target.read_bytes(), ctype)

        def do_POST(self):
            if not self._authorized():
                return self._json({"ok": False, "error": "unauthorized"}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"ok": False, "error": "bad json"}, 400)
            try:
                if self.path == "/api/job":
                    return self._json(app.start_job(body.get("name", "")))
                if self.path == "/api/password":
                    return self._json({"ok": True, "password": app.password_action(body.get("action", ""),
                                                                                   body.get("password"))})
                if self.path == "/api/settings":
                    return self._json({"ok": True, "settings": app.save_settings(body.get("settings") or {})})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)})
            self._json({"ok": False, "error": "not found"}, 404)

    return Handler


def serve(port: int = 8765, open_browser: bool = True, config_path: str | None = None) -> None:
    app = App(config_path)
    httpd = None
    for p in range(port, port + 10):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), make_handler(app))
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("GUI 用のポートを確保できませんでした")
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  画面を開きました → {url}\n  (このウィンドウを閉じると止まります)\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
