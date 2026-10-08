import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml

from swing import config as config_mod
from test_swing_moomoo import NOW, build_fake


@pytest.fixture
def gui(tmp_path, monkeypatch):
    m = build_fake()
    m.OpenQuoteContext.get_global_state = lambda self: (0, {"qot_logined": True, "trd_logined": True,
                                                            "market_us": "CLOSED"})
    m.OpenQuoteContext.get_user_info = lambda self, *a, **k: (0, {"us_qot_right": "LV3"})
    monkeypatch.setitem(sys.modules, "moomoo", m)
    raw = yaml.safe_load(open(config_mod.DEFAULT_PATH, encoding="utf-8"))
    raw["data"]["dir"] = str(tmp_path / "data")
    raw["backtest"]["report_dir"] = str(tmp_path / "reports")
    raw["moomoo_data"]["filter_interval_sec"] = 0
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    # スクリーナーの「今」を固定 (フェイクの日足の最終日に合わせる)
    import swing.screener as scr
    orig = scr.run_weekly_moomoo
    monkeypatch.setattr(scr, "run_weekly_moomoo", lambda cfg, now_jst=None, client=None: orig(cfg, NOW, client))

    from swing import gui as g
    app = g.App(str(cfg_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), g.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None, token=app.token, raw=False):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Token": token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                return r.status, (data if raw else json.loads(data))
        except urllib.error.HTTPError as e:
            return e.code, None

    def wait(name, timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
            _, st = call("/api/status")
            j = st["job"]
            if j and j["name"] == name and j["state"] != "running":
                return st
            time.sleep(0.2)
        raise AssertionError("job timeout")
    yield app, call, wait, base
    httpd.shutdown()


def test_token_required_and_page_served(gui):
    app, call, _, base = gui
    assert call("/api/status", token="bad")[0] == 403
    with urllib.request.urlopen(base + "/") as r:
        assert app.token in r.read().decode()


def test_settings_saved_and_applied(gui):
    app, call, _, _ = gui
    _, r = call("/api/settings", {"settings": {"capital_jpy": "300000", "screener_source": "free", "port": "11112"}})
    assert r["ok"]
    _, st = call("/api/status")
    assert st["settings"]["capital_jpy"] == 300000 and st["settings"]["screener_source"] == "free"
    assert app.cfg()["moomoo"]["port"] == 11112
    _, r = call("/api/settings", {"settings": {"screener_source": "bad"}})
    assert not r["ok"]


def test_check_and_screen_jobs(gui):
    _, call, wait, _ = gui
    assert call("/api/job", {"name": "check"})[1]["ok"]
    st = wait("check")
    assert st["job"]["state"] == "done" and st["job"]["result"]["quota_remain"] == 90
    assert st["job"]["result"]["us_qot_right"] == "LV3"
    assert call("/api/job", {"name": "screen"})[1]["ok"]
    st = wait("screen")
    assert st["job"]["state"] == "done", st["job"]["error"]
    assert [x["symbol"] for x in st["watchlist"]["items"]] == ["GOOD1", "GOOD2"]
    assert any("監視リストを保存" in line["msg"] for line in st["logs"])


def test_one_job_at_a_time_and_unknown_job(gui):
    app, call, wait, _ = gui
    assert not call("/api/job", {"name": "rm -rf"})[1]["ok"]
    ev = threading.Event()
    orig = app._job_check
    app._job_check = lambda: (ev.wait(5), orig())[1]
    assert call("/api/job", {"name": "check"})[1]["ok"]
    r = call("/api/job", {"name": "screen"})[1]
    assert not r["ok"] and "実行中" in r["error"]
    ev.set()
    wait("check")


def test_synthetic_backtest_and_report_files(gui):
    _, call, wait, _ = gui
    assert call("/api/job", {"name": "backtest_synthetic"})[1]["ok"]
    st = wait("backtest_synthetic", timeout=300)
    assert st["job"]["state"] == "done", st["job"]["error"]
    rep = st["report"]
    assert "full_on" in rep["stats"] and rep["labels"]["full_on"].startswith("全期間") and "equity.png" in rep["images"]
    code, png = call(f"/reports/{rep['dir']}/equity.png", raw=True)
    assert code == 200 and png[:4] == b"\x89PNG"
    assert call("/reports/../config.yaml", raw=True)[0] == 404          # 外のファイルは読めない
    assert call(f"/reports/{rep['dir']}/../../config.yaml", raw=True)[0] == 404


def test_trade_tab_status_resume_and_real_refused(gui):
    app, call, wait, _ = gui
    from swing import executor
    _, st = call("/api/status")
    t = st["trade"]
    assert t["env_config"] == "SIMULATE" and not t["halt"]["on"] and t["next_session"]["open_jst"]
    # 停止 → 画面から解除
    cfg = app.cfg()
    s = executor.load_state(cfg, "SIMULATE")
    s["halt"] = {"on": True, "reason": "テスト", "time": "x"}
    executor.save_state(cfg, "SIMULATE", s)
    assert call("/api/status")[1]["trade"]["halt"]["reason"] == "テスト"
    assert call("/api/trade_resume", {})[1]["ok"]
    assert not call("/api/status")[1]["trade"]["halt"]["on"]
    # config が REAL でも、画面からは発注しない
    app.cfg = lambda: config_mod.load(app.config_path, {"moomoo": {"trd_env": "REAL", "allow_real": True}})
    assert call("/api/job", {"name": "trade_close"})[1]["ok"]
    st = wait("trade_close")
    assert st["job"]["state"] == "error" and "模擬口座" in st["job"]["error"]


def test_auto_run_toggle_and_schedule_shown(gui):
    app, call, _, _ = gui
    _, st = call("/api/status")
    assert st["auto"]["on"] is False and len(st["auto"]["upcoming"]) == 3
    assert call("/api/auto", {"on": True})[1]["on"] is True
    assert call("/api/status")[1]["auto"]["on"] is True
    from swing import gui as g
    assert g.App(app.config_path).auto_on()            # 画面を開き直してもオンのまま
    call("/api/auto", {"on": False})
