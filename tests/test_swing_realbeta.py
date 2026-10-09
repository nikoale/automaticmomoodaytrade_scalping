"""本番口座の少額ベータ: 二重ロック (許可 + 起動ごとのロック解除) と、許可の条件。"""
import json
import sys

import pytest
import yaml

from swing import config as config_mod
from swing import realbeta
from test_swing_passwords import SECRET, _fake_moomoo, fake_keyring


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("MOOMOO_TRADE_PASSWORD", raising=False)
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring())
    monkeypatch.setitem(sys.modules, "moomoo", _fake_moomoo())
    raw = yaml.safe_load(open(config_mod.DEFAULT_PATH, encoding="utf-8"))
    raw["data"]["dir"] = str(tmp_path / "data")
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    from swing import gui as g
    return g.App(str(p))


def _cap(app, regular=True, ok=True):
    cfg = app.cfg()
    p = cfg.path("trade", "capability_SIMULATE.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"time": "2026-10-09T23:00:00+09:00", "regular_hours": regular, "ok": ok,
                             "steps": [{"name": "逆指値の売り (GTC)", "ok": ok}]}), encoding="utf-8")


def test_cannot_allow_without_simulate_stop_check_and_password(app):
    with pytest.raises(RuntimeError, match="発注機能の確認"):
        app.real_action("allow", realbeta.PHRASE)
    _cap(app, regular=False)
    with pytest.raises(RuntimeError, match="取引時間中"):
        app.real_action("allow", realbeta.PHRASE)
    _cap(app)
    with pytest.raises(ValueError, match="確認の文"):
        app.real_action("allow", "はい")
    r = app.real_action("allow", realbeta.PHRASE)
    assert r["allowed"] and not r["unlocked"] and r["env"] == "SIMULATE"


def test_double_lock_and_restart_relocks(app):
    _cap(app)
    app.passwords.use_for_session(SECRET)
    app.real_action("allow", realbeta.PHRASE)
    # 許可だけでは本番にならないし、模擬口座の注文も出さない
    assert app.trade_env() == "SIMULATE"
    with pytest.raises(RuntimeError, match="ロック"):
        app._job_trade("close")
    r = app.real_action("unlock", realbeta.PHRASE)               # 取引パスワードでロック解除できるか確かめる
    assert r["unlocked"] and app.trade_env() == "REAL"
    c = app.trade_cfg()
    assert c["moomoo"]["trd_env"] == "REAL" and c["account"]["capital_jpy"] == 50000 and c["executor"]["max_order_jpy"] == 30000
    assert app.cfg()["moomoo"]["trd_env"] == "SIMULATE"          # 元の設定は変わらない
    with pytest.raises(RuntimeError, match="模擬口座だけ"):
        app._job_trade("check")
    # 起動し直すとロックに戻る
    from swing import gui as g
    app2 = g.App(app.config_path)
    assert app2.trade_env() == "SIMULATE" and realbeta.load(app2.cfg())["allowed"]
    app.real_action("lock")
    assert app.trade_env() == "SIMULATE"
    app.real_action("disallow")
    assert not realbeta.load(app.cfg())["allowed"]


def test_wrong_password_does_not_unlock(app):
    _cap(app)
    app.passwords.use_for_session("wrong")
    app.real_action("allow", realbeta.PHRASE)
    with pytest.raises(RuntimeError, match="ロック解除できません"):
        app.real_action("unlock", realbeta.PHRASE)
    assert app.trade_env() == "SIMULATE"


def test_amount_limits(app):
    with pytest.raises(ValueError):
        app.real_action("settings", capital_jpy=500000, max_order_jpy=30000)       # 上限 20 万円を超える
    with pytest.raises(ValueError):
        app.real_action("settings", capital_jpy=50000, max_order_jpy=60000)        # 1 注文 > 資金
    r = app.real_action("settings", capital_jpy=80000, max_order_jpy=40000)
    assert r["capital_jpy"] == 80000 and r["max_order_jpy"] == 40000


def test_order_size_capped_by_max_order(tmp_path):
    from test_swing_executor import CLOSE_RUN, FakeBroker, FakeData, frames_breakout
    from swing import executor, fx
    from swing_helpers import cfg as make_cfg
    import test_swing_executor as te
    c = make_cfg(data={"dir": str(tmp_path)}, executor={"max_order_jpy": 10000})
    wl = c.path("watchlists")
    wl.mkdir(parents=True)
    (wl / "watchlist_20261010.json").write_text(json.dumps({"run_date": "2026-10-10", "data_date": te.LAST.isoformat(), "index_ok": True,
                                           "items": [{"rank": 1, "symbol": "AAA"}]}), encoding="utf-8")
    fx.refresh(c, fetch=lambda: 150.0)
    br = FakeBroker()
    ex = executor.Executor(c, br, "SIMULATE", FakeData(frames_breakout()), now=lambda: CLOSE_RUN, sleep=lambda s: None)
    ex.run_close()
    o = ex.st["orders"][0]
    assert o["qty"] >= 1 and o["qty"] * o["ref_price"] * 150 <= 10000 + 1e-6


def test_allow_with_skipped_simulate_check(app):
    app.passwords.use_for_session(SECRET)
    with pytest.raises(RuntimeError, match="発注機能の確認"):
        app.real_action("allow", realbeta.PHRASE)
    r = app.real_action("allow", realbeta.PHRASE, skip_sim_check=True)        # あなたの判断で省略
    assert r["allowed"] and r["preconditions"][0]["ok"] and "省略" in r["preconditions"][0]["detail"]
    assert app.real_action("unlock", realbeta.PHRASE)["env"] == "REAL"
    app.real_action("disallow")
    assert not realbeta.load(app.cfg()).get("skip_sim_check")


GUI_ERR = "OpenD画面右上のロック解除ボタンから取引のロック解除をしてください。GUI版OpenDではロック解除インターフェースが無効化されています"


def test_gui_opend_unlock_is_done_in_opend(app, monkeypatch):
    """GUI版 OpenD はアプリからロック解除できない (2026-10-09 実機のエラー文)。OpenD の画面で解除してもらう。"""
    from swing import broker
    assert broker.gui_unlock_only(GUI_ERR) and not broker.gui_unlock_only("unlock failed")
    m = _fake_moomoo()
    m.OpenSecTradeContext.unlock_trade = lambda self, password=None, password_md5=None, is_unlock=True: (-1, GUI_ERR)
    monkeypatch.setitem(sys.modules, "moomoo", m)
    app.passwords.use_for_session(SECRET)
    app.real_action("allow", realbeta.PHRASE, skip_sim_check=True)
    r = app.real_action("unlock", realbeta.PHRASE)
    assert r["env"] == "REAL" and "ロック解除" in r["note"]


def test_unlock_without_password_relies_on_opend(app):
    app.real_action("allow", realbeta.PHRASE, skip_sim_check=True)
    r = app.real_action("unlock", realbeta.PHRASE)
    assert r["env"] == "REAL" and r["note"]


def test_broker_real_continues_when_gui_opend(monkeypatch):
    import types
    from swing import broker
    from swing_helpers import cfg as make_cfg

    class Ctx:
        def get_acc_list(self):
            import pandas as pd
            return 0, pd.DataFrame([{"acc_id": 9, "trd_env": "REAL", "trdmarket_auth": ["US"]}])

        def unlock_trade(self, password=None, **k):
            return -1, GUI_ERR

        def close(self):
            pass
    m = types.ModuleType("moomoo")
    m.RET_OK = 0
    m.TrdEnv = types.SimpleNamespace(REAL="REAL", SIMULATE="SIMULATE")
    monkeypatch.setitem(sys.modules, "moomoo", m)
    c = make_cfg(moomoo={"trd_env": "REAL", "allow_real": True})
    _, unlock = broker.resolve_env(c, confirm=lambda: True)
    b = broker.Broker(c, "REAL", unlock, password="x", ctx=Ctx())
    assert b.acc_id == 9
    try:
        b._ok(-1, "trade is locked, please unlock", "発注")
    except broker.BrokerError as e:
        assert "OpenD の画面右上" in str(e)
