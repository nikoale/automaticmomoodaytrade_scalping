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
    cfg = build_config({"preset": "crypto", "symbols": "cc.btcusd、 CC.ETHUSD", "strategy": "orb",
                        "account_size": "5000", "risk_pct": "0.5", "mode": "paper"})
    assert cfg.symbols == ["CC.BTCUSD", "CC.ETHUSD"] and cfg.market == "CC"
    assert cfg.strategy.name == "orb" and cfg.strategy.params == {}
    assert cfg.risk.account_size == 5000 and cfg.risk.risk_per_trade == pytest.approx(0.005)
    with pytest.raises(ValueError):
        build_config({"preset": "crypto", "mode": "simulate"})
    with pytest.raises(ValueError):
        build_config({"preset": "us", "mode": "live"})


def test_check_symbols_backtest(gui):
    _, call = gui
    s = {"preset": "us", "symbols": "US.NVDA"}
    _, r = call("/api/check", {"settings": s})
    assert r["ok"] and any("LV3" in line for line in r["lines"])
    _, r = call("/api/symbols", {"settings": {"preset": "crypto"}, "query": "btc"})
    assert [x["code"] for x in r["rows"]] == ["CC.BTCUSD"]
    _, r = call("/api/backtest", {"settings": s, "source": "sample", "days": 3})
    assert r["ok"] and r["stats"]["trades"] == len(r["trades"])


def test_start_state_flatten_stop(gui):
    app, call = gui
    _, r = call("/api/start", {"settings": {"preset": "us", "symbols": "US.NVDA", "mode": "paper"}})
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
