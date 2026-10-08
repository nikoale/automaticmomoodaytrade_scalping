"""ブラウザで操作する画面 (ローカル専用の小さな Web サーバ。標準ライブラリのみ)。

python -m swing gui → http://127.0.0.1:8765 が開く。
- 127.0.0.1 にのみ待ち受け、起動ごとのトークンで他の Web サイトからの操作を防ぐ
- 時間のかかる処理 (データ取得・スクリーナー・バックテスト) は裏で 1 つずつ実行し、ログを画面に流す
- 発注は模擬口座 (SIMULATE) だけ。画面からは本番口座 (REAL) の注文を出せない (起動時の確認を渡さないため)
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
        self.last_check: dict | None = None   # 最後の OpenD 接続チェック
        from .passwords import PasswordStore
        self.passwords = PasswordStore()
        threading.Thread(target=self._refresh_fx, daemon=True, name="fx").start()   # 今の為替 (裏で 1 回)
        from .runner import Scheduler
        self.scheduler = Scheduler(self.cfg, self._sched_run)
        threading.Thread(target=self._auto_loop, daemon=True, name="auto").start()

    # ---------------------------------------------------------------- 自動実行 (フェーズ 4)
    def _auto_path(self) -> Path:
        return config_mod.load(self.config_path).path("gui_auto.json")

    def auto_on(self) -> bool:
        try:
            return bool(json.loads(self._auto_path().read_text(encoding="utf-8")).get("on"))
        except (OSError, ValueError):
            return False

    def set_auto(self, on: bool) -> bool:
        p = self._auto_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"on": bool(on)}), encoding="utf-8")
        log.warning("[自動実行] %s", "オンにしました (この画面を開いている間、予定の時刻に自動で動きます)" if on else "オフにしました")
        return bool(on)

    def _auto_loop(self) -> None:
        while True:
            try:
                if self.auto_on():
                    self.scheduler.tick()
                sec = self.cfg()["schedule"]["tick_sec"]
            except Exception as e:  # noqa: BLE001 - 自動実行のループは止めない
                log.error("[自動実行] %s", e)
                sec = 60
            time.sleep(sec)

    def _sched_run(self, name: str) -> str:
        """スケジューラーから: 画面のジョブとして実行し、終わるまで待つ。"""
        if self.running() or not self.start_job(name).get("ok"):
            return "busy"
        job = self.job
        while job["state"] == "running":
            time.sleep(1)
        if job["state"] == "error":
            return "error"
        halt = (job.get("result") or {}).get("halt") or {}
        if halt.get("on"):
            from .runner import notify
            notify(self.cfg(), "スイング bot: 発注停止", halt.get("reason", ""))
        return "ok"

    def auto_status(self) -> dict:
        from .runner import upcoming
        try:
            up = upcoming(self.cfg(), datetime.now(calendar_us.TOKYO))
        except Exception as e:  # noqa: BLE001
            up = [{"label": "予定を計算できません", "time": str(e)}]
        return {"on": self.auto_on(), "upcoming": up, "done": self.scheduler.done(), "last": self.scheduler.last}

    def _refresh_fx(self) -> None:
        from . import fx
        try:
            fx.refresh(self.cfg())
        except Exception:  # noqa: BLE001 - 取れなくても画面は動く
            pass

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
                "backtest": lambda: self._job_backtest(False), "backtest_synthetic": lambda: self._job_backtest(True),
                "trade_close": lambda: self._job_trade("close"), "trade_open": lambda: self._job_trade("open"),
                "trade_check": lambda: self._job_trade("check"), "trade_reset": lambda: self._job_trade("reset")}
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
        from . import fx
        from .moomoo_data import MoomooData
        cfg = self.cfg()
        fx.refresh(cfg)
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
        res["time"] = datetime.now(calendar_us.TOKYO).strftime("%H:%M")
        self.last_check = res
        log.info("ログイン: 相場=%s 取引=%s / 米国株の相場権限=%s / 過去K線の取得枠: 使用 %d・残り %d",
                 "OK" if res["qot_logined"] else "NG", "OK" if res["trd_logined"] else "NG",
                 res["us_qot_right"], used, remain)
        return res

    def _job_account(self) -> dict:
        from . import fx
        from .account import AccountReader
        fx.refresh(self.cfg())
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

    def _job_trade(self, what: str) -> dict:
        from . import executor
        cfg = self.cfg()
        if str(cfg["moomoo"]["trd_env"]).upper() != "SIMULATE":
            raise RuntimeError("画面からは模擬口座 (SIMULATE) でしか発注しません。config の moomoo.trd_env を確認してください")
        with executor.Session(cfg) as ex:          # confirm を渡さない → REAL は開けない
            if what == "close":
                s = ex.run_close()
            elif what == "open":
                s = ex.run_open()
            elif what == "reset":
                return ex.reset()
            else:
                s = executor.capability_check(cfg, ex.broker)
        return {"halt": s.get("halt"), "ok": s.get("ok")}

    def trade_resume(self) -> dict:
        from . import executor
        cfg = self.cfg()
        st = executor.load_state(cfg, "SIMULATE")
        if st["halt"]["on"]:
            log.warning("[停止解除] 画面から解除しました (理由だったもの: %s)", st["halt"]["reason"])
            st["halt"] = {"on": False, "reason": "", "time": None}
            executor.save_state(cfg, "SIMULATE", st)
        return st["halt"]

    def trade_status(self) -> dict:
        from . import executor
        cfg = self.cfg()
        try:
            t = executor.read_summary(cfg, "SIMULATE")
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        now = datetime.now(calendar_us.TOKYO)
        d = now.astimezone(calendar_us.NY).date()
        if not calendar_us.is_trading_day(d) or now >= calendar_us.session_times_jst(d)[1]:
            d = calendar_us.next_trading_day(d)
        o, c = calendar_us.session_times_jst(d)
        t["next_session"] = {"date": d.isoformat(), "open_jst": o.strftime("%m/%d %H:%M"), "close_jst": c.strftime("%m/%d %H:%M"),
                             "in_session": o <= now < c}
        t["env_config"] = str(cfg["moomoo"]["trd_env"]).upper()
        t["order_timing"] = cfg["executor"]["order_timing"]
        from . import fx
        t["fx"], t["fx_source"] = fx.current(cfg)
        return t

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
                "password": self.passwords.status(), "trade": self.trade_status(), "auto": self.auto_status(),
                "check": self.last_check,
                "data": self.data_status(), "watchlist": self.latest_watchlist(), "report": self.latest_report(),
                "now_jst": datetime.now(calendar_us.TOKYO).strftime("%Y-%m-%d %H:%M")}


JOB_LABELS = {"check": "OpenD 接続チェック", "account": "口座の読み込み", "verify_password": "取引パスワードの確認", "screen": "今週の監視リスト作成", "fetch": "過去データの取得",
              "backtest": "バックテスト", "backtest_synthetic": "バックテスト (擬似データ)",
              "trade_close": "引け後の処理 (模擬口座)", "trade_open": "寄り付き後の処理 (模擬口座)",
              "trade_check": "発注機能の確認 (模擬口座)", "trade_reset": "模擬口座の記録をリセット"}


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
                if self.path == "/api/auto":
                    return self._json({"ok": True, "on": app.set_auto(bool(body.get("on")))})
                if self.path == "/api/trade_resume":
                    if app.running():
                        return self._json({"ok": False, "error": "処理の実行中は解除できません"})
                    return self._json({"ok": True, "halt": app.trade_resume()})
                if self.path == "/api/settings":
                    return self._json({"ok": True, "settings": app.save_settings(body.get("settings") or {})})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)})
            self._json({"ok": False, "error": "not found"}, 404)

    return Handler


# ---------------------------------------------------------------- 起動 (1 つだけ)
def _instance_path(config_path: str | None) -> Path:
    return config_mod.load(config_path).path("gui_instance.json")


def running_instance(config_path: str | None = None) -> str | None:
    """すでに起動している画面があれば、その URL (2 つ動かすと自動実行が二重になるため)。"""
    import os
    import urllib.request
    try:
        info = json.loads(_instance_path(config_path).read_text(encoding="utf-8"))
        os.kill(int(info["pid"]), 0)                         # プロセスが生きているか
        with urllib.request.urlopen(info["url"], timeout=3) as r:
            if r.status == 200:
                return info["url"]
    except Exception:  # noqa: BLE001 - 古い記録・止まっている → 起動していない扱い
        return None
    return None


def make_server(port: int = 8765, config_path: str | None = None):
    import os
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
        raise SystemExit("画面用のポートを確保できませんでした")
    url = f"http://127.0.0.1:{port}/"
    ip = _instance_path(config_path)
    ip.parent.mkdir(parents=True, exist_ok=True)
    ip.write_text(json.dumps({"pid": os.getpid(), "url": url}), encoding="utf-8")
    return app, httpd, url


def _forget_instance(config_path: str | None) -> None:
    try:
        _instance_path(config_path).unlink()
    except OSError:
        pass


def serve(port: int = 8765, open_browser: bool = True, config_path: str | None = None) -> None:
    other = running_instance(config_path)
    if other:
        print(f"\n  すでに起動しています → {other} を開きます\n")
        if open_browser:
            webbrowser.open(other)
        return
    app, httpd, url = make_server(port, config_path)
    print(f"\n  画面を開きました → {url}\n  (このウィンドウを閉じると止まります)\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        _forget_instance(config_path)


def run_app(config_path: str | None = None) -> None:
    """Mac アプリ (専用のウィンドウ)。pywebview がなければブラウザで開く。"""
    import os
    import subprocess
    import sys
    url = running_instance(config_path)
    httpd = None
    if url is None:
        app, httpd, url = make_server(8765, config_path)
        threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
        if sys.platform == "darwin":               # アプリが動いている間は Mac を眠らせない (電源接続時)
            try:
                subprocess.Popen(["caffeinate", "-is", "-w", str(os.getpid())])
            except OSError:
                pass
    try:
        import webview
    except ImportError:
        log.warning("pywebview がないのでブラウザで開きます")
        webbrowser.open(url)
        if httpd is not None:
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
        return
    _set_dock_icon()
    webview.create_window("米国株スイング bot", url, width=1320, height=880, min_size=(900, 600),
                          confirm_close=httpd is not None)
    try:
        webview.start()
    finally:
        if httpd is not None:
            httpd.shutdown()
            _forget_instance(config_path)


def _set_dock_icon() -> None:
    """Dock のアイコンをアプリのアイコンにする (Python のロケットのアイコンにならないように)。"""
    import os
    icns = os.environ.get("SWING_APP_ICON")
    if not icns or not os.path.exists(icns):
        return
    try:
        from AppKit import NSApplication, NSImage
        NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(icns))
    except Exception:  # noqa: BLE001
        pass
