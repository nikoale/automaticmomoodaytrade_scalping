"""実口座 / 模擬口座 (MoomooBroker) を使うときの安全装置のテスト。fake の口座で検証する。"""
import sys
from datetime import datetime

import pytest

import fake_moomoo
from helpers import ScriptedStrategy, bars, make_cfg

from scalper.engine import SymbolEngine
from scalper.models import Signal
from scalper.risk import RiskManager


@pytest.fixture
def account(monkeypatch):
    def make(fill_mode="full", positions=None, power=1e9, **exec_kw):
        m = fake_moomoo.build(fill_mode, positions={} if positions is None else positions, power=power)
        monkeypatch.setitem(sys.modules, "moomoo", m)
        monkeypatch.setenv("MOOMOO_TRADE_PASSWORD", "pw")
        from scalper.moomoo_broker import MoomooBroker
        cfg = make_cfg()
        cfg.mode = "live"
        cfg.execution.order_timeout_sec = 0.2
        for k, v in exec_kw.items():
            setattr(cfg.execution, k, v)
        risk = RiskManager(cfg.risk)
        broker = MoomooBroker(cfg, on_halt=risk.halt)
        ctx = m.OpenSecTradeContext.instances[-1]
        return cfg, risk, broker, ctx
    return make


def _engine(cfg, risk, broker, signals=None, **kw):
    return SymbolEngine("US.TEST", cfg, ScriptedStrategy(signals or {0: Signal.LONG}, **kw), risk, broker)


def test_entry_places_gtc_protective_stop_and_exit_cancels_it_first(account):
    cfg, risk, broker, ctx = account()
    eng = _engine(cfg, risk, broker)
    eng.on_bar(bars([1000])[0], intrabar_exits=False)
    p = eng.position
    assert p is not None and p.protect_id is not None
    pid = p.protect_id
    stop_order = ctx.orders[pid]
    assert stop_order["type"] == "STOP" and stop_order["side"] == "SELL" and stop_order["stop"] == p.stop
    assert ctx.placed[-1]["time_in_force"] == "GTC"
    assert ctx.holdings["US.TEST"] == 100
    eng.check_price(p.target + 1, datetime(2026, 1, 5, 10, 1))      # 利確
    assert eng.position is None and ctx.holdings["US.TEST"] == 0     # 二重に売っていない
    assert pid in ctx.cancelled
    assert eng.trades[-1].reason == "target"


def test_trailing_moves_protective_stop(account):
    cfg, risk, broker, ctx = account()
    cfg.exits.trail_atr = 1.0
    cfg.exits.target_atr = 100
    eng = _engine(cfg, risk, broker)
    eng.on_bar(bars([1000])[0], intrabar_exits=False)
    eng.on_bar(bars([(1000, 1050, 1000, 1045)], start=datetime(2026, 1, 5, 10, 1))[0], intrabar_exits=False)
    assert eng.position.stop == 1040
    assert ctx.modified and ctx.modified[-1][3] == 1040


def test_exit_not_filled_restores_protection(account):
    cfg, risk, broker, ctx = account()
    eng = _engine(cfg, risk, broker)
    eng.on_bar(bars([1000])[0], intrabar_exits=False)
    first = eng.position.protect_id
    # 以降の通常注文は約定しない
    orig = ctx.place_order

    def no_fill(price, qty, code, trd_side, order_type="NORMAL", **kw):
        ret, df = orig(price, qty, code, trd_side, order_type, **kw)
        oid = str(df["order_id"].iloc[0])
        if order_type != "STOP":
            o = ctx.orders[oid]
            ctx._apply(code, trd_side, -o["dealt"] if trd_side == "BUY" else o["dealt"])  # 約定を取り消す
            o.update(status="SUBMITTED", dealt=0, avg=0.0)
        return ret, df
    ctx.place_order = no_fill
    eng.check_price(980, datetime(2026, 1, 5, 10, 1))                # 損切りしたいが約定しない
    assert eng.position is not None
    assert first in ctx.cancelled
    new = eng.position.protect_id
    assert new is not None and new != first and ctx.orders[new]["status"] == "SUBMITTED"


def test_reconcile_detects_protective_stop_fill(account):
    cfg, risk, broker, ctx = account()
    from scalper.live import LiveRunner
    runner = LiveRunner(cfg)
    runner.broker, runner.risk = broker, risk
    eng = _engine(cfg, risk, broker)
    runner.engines["US.TEST"] = eng
    eng.on_bar(bars([1000])[0], intrabar_exits=False)
    pid = eng.position.protect_id
    ctx.trigger_stop(pid, 989.5)               # Mac がスリープ中に口座側で損切りされた
    runner._reconcile()
    assert eng.position is not None            # 1 回目は反映待ちとして様子見
    runner._reconcile()
    assert eng.position is None
    t = eng.trades[-1]
    assert t.reason == "protective_stop" and t.exit_price == 989.5
    assert risk.trades_today == 1 and risk.daily_pnl < 0


def test_reconcile_blocks_on_unexplained_mismatch(account):
    cfg, risk, broker, ctx = account()
    from scalper.live import LiveRunner
    runner = LiveRunner(cfg)
    runner.broker, runner.risk = broker, risk
    eng = _engine(cfg, risk, broker, signals={})
    runner.engines["US.TEST"] = eng
    ctx.holdings["US.TEST"] = 50               # ボットの知らない株がある (取消後の約定など)
    runner._reconcile()
    runner._reconcile()
    assert "US.TEST" in runner._blocked and not eng.enabled
    assert risk.halted_reason and "一致しません" in risk.halted_reason


def test_cleanup_stale_orders_keeps_protection_for_held_symbols(account):
    cfg, risk, broker, ctx = account(positions={"US.HELD": 10})
    ctx.place_order(price=90, qty=10, code="US.HELD", trd_side="SELL", order_type="STOP", aux_price=90,
                    remark="scalper:protect")
    ctx.place_order(price=50, qty=5, code="US.FLAT", trd_side="SELL", order_type="STOP", aux_price=50,
                    remark="scalper:protect")
    ctx.place_order(price=60, qty=5, code="US.FLAT", trd_side="SELL", order_type="STOP", aux_price=60,
                    remark="my manual order")     # 自分で出した注文には触らない
    from scalper.live import LiveRunner
    runner = LiveRunner(cfg)
    runner.broker = broker
    runner._cleanup_stale_orders()
    assert ctx.cancelled == ["2"]


def test_order_rate_kill_switch(account):
    cfg, risk, broker, ctx = account(max_orders_per_minute=2)
    from scalper.models import Side
    for _ in range(3):
        broker.execute("US.TEST", Side.BUY, 1, 100, datetime.now(), "entry_long")
    normal = [p for p in ctx.placed if p["order_type"] != "STOP"]
    assert len(normal) == 2 and risk.halted_reason and "暴走" in risk.halted_reason


def test_buying_power_caps_quantity(account):
    cfg, risk, broker, ctx = account(power=50_000)
    eng = _engine(cfg, risk, broker)
    eng.on_bar(bars([1000])[0], intrabar_exits=False)
    # リスク基準では 100 株だが、余力 5 万 × 95% / 1000 = 47 → 売買単位 100 では 0 → 見送り
    assert eng.position is None
    cfg.risk.lot_size = 1
    eng2 = SymbolEngine("US.TEST", cfg, ScriptedStrategy({0: Signal.LONG}), risk, broker, lot_size=1)
    eng2.on_bar(bars([1000])[0], intrabar_exits=False)
    assert eng2.position.qty == 47


def test_fee_filter_skips_small_targets():
    from scalper.broker import SimBroker
    cfg = make_cfg()
    cfg.execution.commission_rate = 0.00132
    cfg.exits.min_reward_cost_ratio = 2.0
    risk = RiskManager(cfg.risk)
    # 株価 1000, ATR 1 → 利確 2 ドル。往復手数料 2.64 ドル × 2 = 5.28 に届かないので見送り
    eng = SymbolEngine("US.TEST", cfg, ScriptedStrategy({0: Signal.LONG}, atr=1.0), risk,
                       SimBroker(cfg.execution, "US"))
    eng.on_bar(bars([1000])[0])
    assert eng.position is None
    eng = SymbolEngine("US.TEST", cfg, ScriptedStrategy({0: Signal.LONG}, atr=10.0), risk,
                       SimBroker(cfg.execution, "US"))
    eng.on_bar(bars([1000])[0])
    assert eng.position is not None


def test_commission_rate_and_cap():
    from scalper.broker import SimBroker
    cfg = make_cfg()
    cfg.execution.commission_rate = 0.00132
    b = SimBroker(cfg.execution, "US")
    assert b.commission(100, 10, cfg.execution) == pytest.approx(1.32)
    assert b.commission(100, 1000, cfg.execution) == 22.0      # 上限 22 USD
