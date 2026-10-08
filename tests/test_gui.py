import json
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import fake_moomoo


@pytest.fixture
def gui(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "moomoo", fake_moomoo.build("full"))
    from scalper.gui import server
    monkeypatch.setattr(server, "SETTINGS_FILE", tmp_path / "gui.json")
    app = server.App()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None, token=app.token):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Token": token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    yield app, call
    app.stop()
    httpd.shutdown()


def test_api_requires_token(gui):
    _, call = gui
    assert call("/api/meta", token="wrong")[0] == 403
    status, meta = call("/api/meta")
    assert status == 200 and "us" in meta["presets"] and "ema_vwap" in meta["strategies"]


def test_build_config_from_form():
    from scalper.gui.server import build_config
    cfg = build_config({"preset": "us", "symbols": "us.nvda、 US.AMD", "strategy": "orb",
                        "account_size": "5000", "risk_pct": "0.5", "mode": "paper"})
    assert cfg.symbols == ["US.NVDA", "US.AMD"] and cfg.market == "US"
    assert cfg.strategy.name == "orb" and cfg.strategy.params == {}
    assert cfg.risk.account_size == 5000 and cfg.risk.risk_per_trade == pytest.approx(0.005)
    with pytest.raises(ValueError):
        build_config({"preset": "us_ext", "mode": "simulate"})     # 時間外は paper のみ
    with pytest.raises(ValueError):
        build_config({"preset": "us", "mode": "live"})


def test_check_symbols_backtest(gui):
    _, call = gui
    s = {"preset": "us", "symbols": "US.NVDA"}
    _, r = call("/api/check", {"settings": s})
    assert r["ok"] and any("LV3" in line for line in r["lines"])
    _, r = call("/api/symbols", {"settings": {"preset": "us"}, "query": "nvidia"})
    assert [x["code"] for x in r["rows"]] == ["US.NVDA"]
    _, r = call("/api/backtest", {"settings": s, "source": "sample", "days": 3})
    assert r["ok"] and r["stats"]["trades"] == len(r["trades"])


def test_start_state_flatten_stop(gui):
    app, call = gui
    _, r = call("/api/start", {"settings": {"preset": "us", "symbols": "US.NVDA", "mode": "paper", "auto": False}})
    assert r["ok"]
    for _ in range(50):
        _, st = call("/api/state?since=0")
        if st["phase"] == "running":
            break
        time.sleep(0.1)
    assert st["running"] and st["phase"] == "running" and st["symbols"][0]["code"] == "US.NVDA"
    assert st["logs"]
    assert call("/api/start", {"settings": {"preset": "us"}})[1]["ok"] is False   # 二重起動しない
    assert call("/api/command", {"cmd": "flatten"})[1]["ok"]
    assert call("/api/stop", {})[1]["ok"]
    _, st = call("/api/state?since=0")
    assert not st["running"] and st["phase"] == "stopped"


def test_screen_endpoint_marks_chosen(gui):
    _, call = gui
    _, r = call("/api/screen", {"settings": {"preset": "us", "auto": True, "auto_count": 2}})
    assert r["ok"] and sum(1 for x in r["rows"] if x["chosen"]) == 2


def test_build_config_auto():
    from scalper.gui.server import build_config
    cfg = build_config({"preset": "us", "auto": True, "auto_count": "4"})
    assert cfg.auto_symbols.enabled and cfg.auto_symbols.count == 4


def test_live_requires_confirmation_and_password_never_saved(gui, tmp_path):
    from scalper.gui import server
    from scalper.gui.server import build_config
    base = {"preset": "us", "mode": "live", "symbols": "US.NVDA", "auto": False}
    with pytest.raises(ValueError, match="理解しました"):
        build_config({**base, "password": "pw"})
    with pytest.raises(ValueError, match="パスワード"):
        build_config({**base, "confirm_live": True})
    with pytest.raises(ValueError, match="時間外"):
        build_config({**base, "preset": "us_ext", "confirm_live": True, "password": "pw"})
    cfg = build_config({**base, "confirm_live": True, "password": "secret-pw"})
    assert cfg.mode == "live" and cfg.trade_password() == "secret-pw"
    app, call = gui
    app.save_settings({**base, "confirm_live": True, "password": "secret-pw"})
    saved = server.SETTINGS_FILE.read_text(encoding="utf-8")
    assert "secret-pw" not in saved and "confirm_live" not in saved


def test_live_start_via_api_unlocks_and_runs(gui):
    app, call = gui
    s = {"preset": "us", "mode": "live", "symbols": "US.NVDA", "auto": False, "confirm_live": True,
         "password": "pw123"}
    _, r = call("/api/check", {"settings": s})
    assert r["ok"] and any("実口座" in line for line in r["lines"])
    _, r = call("/api/start", {"settings": s})
    assert r["ok"]
    for _ in range(50):
        _, st = call("/api/state?since=0")
        if st["phase"] == "running":
            break
        time.sleep(0.1)
    assert st["phase"] == "running" and st["mode"] == "live"
    ctx = sys.modules["moomoo"].OpenSecTradeContext.instances[-1]
    assert ctx.unlocked == "pw123"
    assert not any("pw123" in line["msg"] for line in st["logs"])     # ログにパスワードが出ない
    assert call("/api/stop", {})[1]["ok"]
