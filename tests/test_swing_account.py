"""口座の読み込み (読むだけ) とボットが使う資金のテスト。"""
import sys
import types

import pandas as pd
import pytest

from swing.account import effective_capital
from swing_helpers import cfg as make_cfg
from test_swing_moomoo import build_fake

FX = 150.0


def add_trade_ctx(m, accounts=None, usd_total=2000.0, positions=True):
    m.TrdEnv = types.SimpleNamespace(REAL="REAL", SIMULATE="SIMULATE")
    m.TrdMarket = types.SimpleNamespace(US="US")
    m.SecurityFirm = types.SimpleNamespace(FUTUJP="FUTUJP")
    m.Currency = types.SimpleNamespace(USD="USD", JPY="JPY")
    accounts = accounts if accounts is not None else [
        {"acc_id": 111, "trd_env": "REAL", "acc_type": "CASH", "trdmarket_auth": ["JP"]},
        {"acc_id": 222, "trd_env": "REAL", "acc_type": "CASH", "trdmarket_auth": ["US", "JP"]},
        {"acc_id": 333, "trd_env": "SIMULATE", "acc_type": "CASH", "trdmarket_auth": ["US"]}]

    class TradeCtx:
        last = None

        def __init__(self, **kw):
            self.kw, self.calls = kw, []
            TradeCtx.last = self

        def get_acc_list(self):
            return 0, pd.DataFrame(accounts)

        def accinfo_query(self, trd_env, acc_id, refresh_cache=False, currency="USD", **kw):
            self.calls.append(("accinfo", trd_env, acc_id, currency))
            k = 1.0 if currency == "USD" else FX
            return 0, pd.DataFrame([{"total_assets": usd_total * k, "cash": 500 * k, "market_val": 1500 * k,
                                     "power": 480 * k, "avl_withdrawal_cash": 450 * k, "unrealized_pl": 120 * k,
                                     "realized_pl": -30 * k}])

        def position_list_query(self, trd_env, acc_id, position_market=None, currency="USD", refresh_cache=False, **kw):
            self.calls.append(("positions", trd_env, acc_id))
            rows = [{"code": "US.AAPL", "stock_name": "Apple", "qty": 5, "average_cost": 200.0, "cost_price": 200.0,
                     "nominal_price": 220.0, "market_val": 1100.0, "pl_val": 100.0, "pl_ratio": 10.0, "today_pl_val": 5.0,
                     "position_side": "LONG"},
                    {"code": "US.OLD", "stock_name": "Sold", "qty": 0, "market_val": 0, "pl_val": 0}] if positions else []
            return 0, pd.DataFrame(rows)

        def __getattr__(self, name):          # 注文・ロック解除などを呼んだらテスト失敗
            if name in ("unlock_trade", "place_order", "modify_order", "cancel_all_order"):
                raise AssertionError(f"読むだけのはずが {name} を呼んだ")
            raise AttributeError(name)

        def close(self):
            pass
    m.OpenSecTradeContext = TradeCtx
    return m


@pytest.fixture
def fake(monkeypatch):
    def go(**kw):
        m = add_trade_ctx(build_fake(), **kw)
        monkeypatch.setitem(sys.modules, "moomoo", m)
        return m
    return go


def test_snapshot_jpy_and_usd(fake):
    m = fake()
    from swing.account import AccountReader
    with AccountReader(make_cfg(), "REAL") as r:
        snap = r.snapshot()
    assert snap["acc_id"] == 222                                    # 米国株の権限がある実口座
    assert snap["fx"] == pytest.approx(FX) and "口座" in snap["fx_source"]
    t = snap["summary"]["total_assets"]
    assert t["usd"] == 2000 and t["jpy"] == 2000 * FX
    assert [p["code"] for p in snap["positions"]] == ["US.AAPL"]     # 株数 0 は出さない
    p = snap["positions"][0]
    assert p["market_val_jpy"] == pytest.approx(1100 * FX) and p["pl_jpy"] == pytest.approx(100 * FX)
    calls = m.OpenSecTradeContext.last.calls
    assert ("accinfo", "REAL", 222, "USD") in calls and ("accinfo", "REAL", 222, "JPY") in calls


def test_simulate_account_and_missing_account(fake):
    fake()
    from swing.account import AccountReader
    with AccountReader(make_cfg(), "SIMULATE") as r:
        assert r.snapshot()["acc_id"] == 333
    fake(accounts=[{"acc_id": 1, "trd_env": "SIMULATE", "trdmarket_auth": ["US"]}])
    with AccountReader(make_cfg(), "REAL") as r, pytest.raises(RuntimeError, match="見つかりません"):
        r.snapshot()


def test_effective_capital_rules():
    c = make_cfg(account={"capital_jpy": 200000, "fx_rate_jpy_per_usd": 150.0, "capital_source": "account"})
    e = effective_capital(c, account_total_usd=5000.0, fx=150.0)
    assert e["usd"] == pytest.approx(200000 / 150) and "上限" in e["reason"]      # 口座が多くても設定額まで
    e = effective_capital(c, account_total_usd=800.0, fx=150.0)
    assert e["usd"] == 800.0 and e["jpy"] == 120000.0 and "口座の総資産" in e["reason"]
    e = effective_capital(c, None)
    assert e["usd"] == pytest.approx(200000 / 150) and "読み込む" in e["reason"]
    c2 = make_cfg(account={"capital_source": "config"})
    assert effective_capital(c2, account_total_usd=10.0)["usd"] == pytest.approx(200000 / 150)


def test_gui_account_job(fake, tmp_path, monkeypatch):
    import json
    import threading
    import time
    import urllib.request
    from http.server import ThreadingHTTPServer

    import yaml

    from swing import config as config_mod
    from swing import gui as g
    fake(usd_total=900.0)
    raw = yaml.safe_load(open(config_mod.DEFAULT_PATH, encoding="utf-8"))
    raw["data"]["dir"] = str(tmp_path / "data")
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    app = g.App(str(p))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), g.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Token": app.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    try:
        assert call("/api/job", {"name": "account"})["ok"]
        for _ in range(100):
            st = call("/api/status")
            if st["job"]["state"] != "running":
                break
            time.sleep(0.1)
        assert st["job"]["state"] == "done", st["job"]["error"]
        assert st["account"]["summary"]["total_assets"]["jpy"] == 900 * FX
        assert st["capital"]["usd"] == 900.0                       # 口座の方が少ない → 口座の総資産
        assert not any("900" in line["msg"] or "135" in line["msg"] for line in st["logs"])   # 金額をログに出さない
        # 模擬口座に切り替えたら前の数字は消える
        assert call("/api/settings", {"settings": {"account_env": "SIMULATE"}})["ok"]
        assert call("/api/status")["account"] is None
        assert not (tmp_path / "data" / "gui_settings.json").read_text().count("900")
    finally:
        httpd.shutdown()
