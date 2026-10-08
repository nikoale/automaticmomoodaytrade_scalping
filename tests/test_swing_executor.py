"""フェーズ 3: 発注・リスク管理のテスト (証券会社の偽物で。実機の OpenD では未検証)。"""
import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from swing import broker as broker_mod
from swing import calendar_us, executor
from swing.broker import BrokerError, RealLocked
from swing_helpers import cfg as make_cfg
from swing_helpers import trend_with_breakout

LAST = date(2026, 10, 9)                                              # 金曜
CLOSE_RUN = datetime(2026, 10, 10, 7, 30, tzinfo=calendar_us.TOKYO)    # 土曜の朝 = 金曜の引け後
OPEN_RUN = datetime(2026, 10, 12, 22, 40, tzinfo=calendar_us.TOKYO)    # 月曜 (夏時間: 寄り付き 22:30 JST)
DATES = pd.bdate_range(end=pd.Timestamp(LAST), periods=320)


class FakeBroker:
    """OpenD の注文の動きを真似る。成行・逆指値は test が fill() するまで約定しない。"""
    env_name = "SIMULATE"
    acc_id = 7

    def __init__(self):
        self.book: dict[str, dict] = {}
        self.held: dict[str, float] = {}
        self.calls: list[tuple] = []
        self.n = 0
        self.fail_stop = False
        self.fail_market = False
        self.down = False

    def _chk(self):
        if self.down:
            raise BrokerError("接続が切れました")

    def _new(self, sym, side, typ, qty, rmk, price=None, aux=None):
        self._chk()
        self.n += 1
        oid = f"O{self.n}"
        self.book[oid] = {"order_id": oid, "symbol": sym, "side": side, "type": typ, "status": "SUBMITTED",
                          "qty": qty, "price": price, "aux_price": aux, "dealt_qty": 0.0, "dealt_avg_price": None,
                          "remark": rmk, "err": "", "time": ""}
        self.calls.append(("place", typ, side, sym, qty, aux, rmk))
        return dict(self.book[oid])

    def funds(self):
        self._chk()
        return {"total_assets": 1e6, "cash": 1e6, "power": 1e6, "market_val": 0, "fx": 150.0}

    def positions(self):
        self._chk()
        return {s: {"qty": q} for s, q in self.held.items() if q}

    def orders(self):
        self._chk()
        return [dict(o) for o in self.book.values()]

    def order(self, oid):
        self._chk()
        o = self.book.get(str(oid))
        return dict(o) if o else None

    def market(self, sym, side, qty, rmk):
        if self.fail_market:
            raise BrokerError("時間外は成行を受け付けません")
        return self._new(sym, side, "MARKET", qty, rmk)

    def limit(self, sym, side, qty, price, rmk):
        return self._new(sym, side, "NORMAL", qty, rmk, price=price)

    def stop(self, sym, qty, px, rmk):
        if self.fail_stop:
            raise BrokerError("逆指値は使えません")
        return self._new(sym, "SELL", "STOP", qty, rmk, price=round(px, 2), aux=round(px, 2))

    def modify_stop(self, oid, qty, px):
        self._chk()
        self.book[oid]["aux_price"] = round(px, 2)
        self.calls.append(("modify", oid, round(px, 2)))

    def modify_limit(self, oid, qty, px):
        self.book[oid]["price"] = px

    def cancel(self, oid):
        self._chk()
        o = self.book[oid]
        if o["status"] in broker_mod.ACTIVE:
            o["status"] = "CANCELLED_PART" if o["dealt_qty"] else "CANCELLED_ALL"
        self.calls.append(("cancel", oid))

    # テスト用: 約定させる
    def fill(self, oid, price, qty=None):
        o = self.book[oid]
        q = o["qty"] if qty is None else qty
        o["dealt_qty"], o["dealt_avg_price"] = q, price
        o["status"] = "FILLED_ALL" if q >= o["qty"] else "FILLED_PART"
        self.held[o["symbol"]] = self.held.get(o["symbol"], 0) + (q if o["side"] == "BUY" else -q)

    def active(self, typ=None):
        return [o for o in self.book.values() if o["status"] in broker_mod.ACTIVE and (typ is None or o["type"] == typ)]


class FakeData:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames

    def daily_bars(self, sym, start, end):
        df = self.frames.get(sym)
        if df is None:
            raise RuntimeError("no data")
        return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]

    def earnings_calendar(self, b, e):
        return {}


def _frame(close, volume=None, dates=DATES):
    c = np.asarray(close, dtype=float)
    o = np.r_[c[0], c[:-1]]
    v = np.full(len(c), 1e6) if volume is None else np.asarray(volume, dtype=float)
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.01, "low": np.minimum(o, c) * 0.99,
                         "close": c, "volume": v}, index=dates)


def frames_breakout():
    s = trend_with_breakout(len(DATES), len(DATES) - 1)
    return {"SPY": _frame(np.linspace(300, 400, len(DATES))), "AAA": _frame(s["close"], s["volume"])}


@pytest.fixture
def env(tmp_path):
    cfg = make_cfg(data={"dir": str(tmp_path)})
    wl = cfg.path("watchlists")
    wl.mkdir(parents=True)
    (wl / "watchlist_20261010.json").write_text(json.dumps({
        "run_date": "2026-10-10", "data_date": LAST.isoformat(), "index_ok": True, "note": "", "counts": {},
        "unknown_earnings": [], "items": [{"rank": 1, "symbol": "AAA", "next_earnings": None}]}), encoding="utf-8")
    return cfg


def make(cfg, br, frames=None, now=CLOSE_RUN):
    clock = {"now": now}
    ex = executor.Executor(cfg, br, "SIMULATE", FakeData(frames or frames_breakout()), now=lambda: clock["now"],
                           sleep=lambda s: None)
    return ex, clock


def test_entry_is_reserved_once_then_filled_with_broker_stop(env):
    br = FakeBroker()
    ex, clock = make(env, br)
    s = ex.run_close()
    assert not s["halt"]["on"]
    buys = br.active("MARKET")
    assert len(buys) == 1 and buys[0]["symbol"] == "AAA" and buys[0]["side"] == "BUY"
    assert buys[0]["remark"] == "swb:261012:AAA:B"                        # 翌営業日 (月曜) の寄り付き用
    qty = buys[0]["qty"]
    assert qty >= 1 and qty == int(qty)
    assert qty * 21.2 <= 1333.4 * 0.40 + 25                                # 1 銘柄は資金の 40% まで
    # 同じ日にもう一度押しても出し直さない
    ex.run_close()
    executor.Executor(env, br, "SIMULATE", FakeData(frames_breakout()), now=lambda: CLOSE_RUN).run_close(force=True)
    assert len(br.active("MARKET")) == 1

    # 寄り付きで約定 → すぐに証券会社側の逆指値 (GTC)
    br.fill(buys[0]["order_id"], 21.5)
    clock["now"] = OPEN_RUN
    s = ex.run_open()
    assert not s["halt"]["on"], s["halt"]
    stops = br.active("STOP")
    assert len(stops) == 1 and stops[0]["qty"] == qty
    p = s["positions"]["AAA"]
    assert p["stop_order_id"] == stops[0]["order_id"]
    assert stops[0]["aux_price"] == pytest.approx(21.5 - 2 * p["entry_atr"], abs=0.01)
    assert ex.st["cash_settled"] < ex.st["capital_usd"]


def _hold(env, br):
    ex, clock = make(env, br)
    ex.run_close()
    br.fill(br.active("MARKET")[0]["order_id"], 21.5)
    clock["now"] = OPEN_RUN
    ex.run_open()
    return ex, clock


def test_stop_is_raised_by_modifying_broker_order_and_stop_fill_is_booked(env):
    br = FakeBroker()
    ex, clock = _hold(env, br)
    stop_id = br.active("STOP")[0]["order_id"]
    # 翌日は大きく上昇 → トレーリング開始 → 逆指値を訂正 (下げはしない)
    fr = frames_breakout()
    d2 = pd.bdate_range(start=DATES[0], periods=len(DATES) + 1)
    c = list(fr["AAA"]["close"]) + [30.0]
    v = list(fr["AAA"]["volume"]) + [1e6]
    fr = {"SPY": _frame(np.linspace(300, 401, len(d2)), dates=d2), "AAA": _frame(c, v, dates=d2)}
    ex.md = FakeData(fr)
    clock["now"] = datetime(2026, 10, 13, 7, 30, tzinfo=calendar_us.TOKYO)
    ex.run_close()
    mods = [c for c in br.calls if c[0] == "modify"]
    assert mods and mods[-1][1] == stop_id and mods[-1][2] > ex.st["positions"]["AAA"]["entry_price"] - 1
    assert ex.st["positions"]["AAA"]["trailing"]

    # 逆指値が約定 → 建玉を閉じ、売却代金は T+1 で使えるようになる、5 営業日は再エントリーしない
    br.fill(stop_id, 28.0)
    clock["now"] = datetime(2026, 10, 14, 7, 30, tzinfo=calendar_us.TOKYO)
    ex.reconcile()
    assert "AAA" not in ex.st["positions"]
    t = ex.st["trades"][-1]
    assert t["reason"] == "trailing_stop" and t["pnl"] > 0
    assert ex.st["pending_sales"][0][0] == "2026-10-14"                   # 10/13 の約定 → 翌営業日
    assert ex.st["cooldown"]["AAA"] == calendar_us.add_trading_days(date(2026, 10, 13), 5).isoformat()


def test_unfilled_entry_is_cancelled_and_halts(env):
    br = FakeBroker()
    ex, clock = make(env, br)
    ex.run_close()
    clock["now"] = OPEN_RUN
    s = ex.run_open()                                    # 待っても約定しない
    assert s["halt"]["on"] and "約定しない" in s["halt"]["reason"]
    assert not br.active() and not ex.st["orders"]
    # 停止中は新しい注文を出さない
    n = len(br.book)
    clock["now"] = datetime(2026, 10, 13, 7, 30, tzinfo=calendar_us.TOKYO)
    ex.run_close(force=True)
    assert len(br.book) == n


def test_unknown_order_on_same_symbol_is_treated_as_double_order(env):
    br = FakeBroker()
    br._new("AAA", "BUY", "MARKET", 5, "manual")       # ボットが知らない買い注文
    ex, _ = make(env, br)
    s = ex.run_close()
    assert s["halt"]["on"] and "二重発注" in s["halt"]["reason"]
    assert len(br.book) == 1


def test_lost_response_does_not_double_order(env):
    br = FakeBroker()
    real_market = br.market

    def flaky(sym, side, qty, rmk):
        real_market(sym, side, qty, rmk)                # 注文は通ったが応答が届かない
        raise BrokerError("timeout")
    br.market = flaky
    ex, _ = make(env, br)
    ex.run_close()
    assert len(br.active("MARKET")) == 1
    assert ex.st["orders"][0]["order_id"] == br.active("MARKET")[0]["order_id"]


def test_position_mismatch_halts(env):
    br = FakeBroker()
    ex, clock = _hold(env, br)
    br.held["AAA"] = 0                                   # ボットの知らないところで売られた
    clock["now"] = datetime(2026, 10, 13, 7, 30, tzinfo=calendar_us.TOKYO)
    s = ex.run_close()
    assert s["halt"]["on"] and "保有株数" in s["halt"]["reason"]


def test_stop_failure_flattens_and_halts(env):
    br = FakeBroker()
    br.fail_stop = True
    ex, clock = make(env, br)
    ex.run_close()
    br.fill(br.active("MARKET")[0]["order_id"], 21.5)
    clock["now"] = OPEN_RUN
    s = ex.run_open()
    assert s["halt"]["on"] and "逆指値" in s["halt"]["reason"]
    sells = [o for o in br.active("MARKET") if o["side"] == "SELL"]
    assert len(sells) == 1 and sells[0]["qty"] == s["positions"]["AAA"]["shares"]


def test_reserve_rejected_is_placed_at_open(env):
    br = FakeBroker()
    br.fail_market = True
    ex, clock = make(env, br)
    s = ex.run_close()
    assert not s["halt"]["on"] and not br.book and ex.st["orders"][0]["order_id"] is None
    br.fail_market = False
    clock["now"] = OPEN_RUN
    ex.st["orders"][0]  # 寄り付き後に出す
    ex.ex = dict(ex.ex, fill_wait_sec=0)
    ex.run_open()
    assert any(c[1] == "MARKET" and c[2] == "BUY" for c in br.calls if c[0] == "place")


def test_exit_cancels_stop_before_selling(env):
    br = FakeBroker()
    ex, clock = _hold(env, br)
    stop_id = br.active("STOP")[0]["order_id"]
    # SPY が 200 日線を割る → 全手仕舞い
    fr = frames_breakout()
    d2 = pd.bdate_range(start=DATES[0], periods=len(DATES) + 1)
    spy = list(fr["SPY"]["close"]) + [200.0]
    fr = {"SPY": _frame(spy, dates=d2), "AAA": _frame(list(fr["AAA"]["close"]) + [22.0], dates=d2)}
    ex.md = FakeData(fr)
    clock["now"] = datetime(2026, 10, 13, 7, 30, tzinfo=calendar_us.TOKYO)
    ex.run_close()
    i_cancel = br.calls.index(("cancel", stop_id))
    i_sell = next(i for i, c in enumerate(br.calls) if c[0] == "place" and c[1] == "MARKET" and c[2] == "SELL")
    assert i_cancel < i_sell
    assert br.book[stop_id]["status"] == "CANCELLED_ALL"


def test_connection_error_halts(env):
    br = FakeBroker()
    ex, _ = make(env, br)
    ex._init_capital()
    br.down = True
    s = ex.run_close()
    assert s["halt"]["on"] and "通信" in s["halt"]["reason"]


def test_state_survives_restart(env):
    br = FakeBroker()
    ex, clock = _hold(env, br)
    ex2 = executor.Executor(env, br, "SIMULATE", ex.md, now=lambda: OPEN_RUN)
    assert ex2.st["positions"]["AAA"]["stop_order_id"] == br.active("STOP")[0]["order_id"]


# ---------------------------------------------------------------- 本番口座のロック
def test_real_is_double_locked():
    c = make_cfg(moomoo={"trd_env": "REAL", "allow_real": False})
    with pytest.raises(RealLocked, match="allow_real"):
        broker_mod.resolve_env(c, confirm=lambda: True)
    c = make_cfg(moomoo={"trd_env": "REAL", "allow_real": True})
    with pytest.raises(RealLocked, match="確認"):
        broker_mod.resolve_env(c)                          # 画面からは confirm を渡さない
    with pytest.raises(RealLocked):
        broker_mod.resolve_env(c, confirm=lambda: False)
    env, unlock = broker_mod.resolve_env(c, confirm=lambda: True)
    assert env == "REAL" and isinstance(unlock, broker_mod.RealUnlock)
    with pytest.raises(RealLocked):
        broker_mod.RealUnlock(object())
    assert broker_mod.resolve_env(make_cfg()) == ("SIMULATE", None)


def test_broker_refuses_real_without_unlock(monkeypatch):
    import sys
    import types
    m = types.ModuleType("moomoo")
    m.TrdEnv = types.SimpleNamespace(REAL="REAL", SIMULATE="SIMULATE")
    monkeypatch.setitem(sys.modules, "moomoo", m)
    with pytest.raises(RealLocked):
        broker_mod.Broker(make_cfg(), "REAL", None, ctx=object())


def test_capability_check_simulate_only(env):
    br = FakeBroker()
    br.env_name = "REAL"
    with pytest.raises(RuntimeError, match="模擬口座"):
        executor.capability_check(env, br)
    br = FakeBroker()
    res = executor.capability_check(env, br, regular_hours=False, sleep=lambda s: None, now=CLOSE_RUN)
    assert res["ok"], res
    assert res["order_timing_hint"] == "reserve"
    assert not br.active()                               # 確認の注文は全部取り消してある
    assert env.path("trade", "capability_SIMULATE.json").exists()


def test_config_rejects_bad_executor_values():
    with pytest.raises(ValueError, match="order_timing"):
        make_cfg(executor={"order_timing": "whenever"})
