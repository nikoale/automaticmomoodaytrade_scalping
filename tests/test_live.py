import sys
from datetime import datetime

import pytest

import fake_moomoo
from helpers import ScriptedStrategy, make_cfg

from scalper.broker import SimBroker
from scalper.engine import SymbolEngine
from scalper.models import Side, Signal
from scalper.risk import RiskManager


@pytest.fixture
def fake(monkeypatch):
    def install(mode="full"):
        m = fake_moomoo.build(mode)
        monkeypatch.setitem(sys.modules, "moomoo", m)
        return m
    return install


def _cfg(mode="live"):
    cfg = make_cfg()
    cfg.mode = mode
    cfg.execution.order_timeout_sec = 0.3
    cfg.execution.limit_offset_ticks = 1
    return cfg


def test_broker_requires_password_for_real(fake, monkeypatch):
    fake()
    from scalper.moomoo_broker import MoomooBroker
    monkeypatch.delenv("MOOMOO_TRADE_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        MoomooBroker(_cfg())


def test_broker_full_fill_uses_aggressive_limit(fake, monkeypatch):
    m = fake()
    from scalper.moomoo_broker import MoomooBroker
    monkeypatch.setenv("MOOMOO_TRADE_PASSWORD", "x")
    b = MoomooBroker(_cfg(), best_quote=lambda c: (999.0, 1000.0))
    f = b.execute("JP.7203", Side.BUY, 100, 1000, datetime.now(), "entry_long")
    ctx = m.OpenSecTradeContext.instances[-1]
    assert ctx.unlocked == "x"
    assert ctx.placed[0]["price"] == 1001 and ctx.placed[0]["jp_acc_type"] == "JP_TOKUTEI"
    assert f.qty == 100 and f.price == 1001
    f = b.execute("JP.7203", Side.SELL, 100, 1000, datetime.now(), "stop")
    assert ctx.placed[1]["price"] == 998


def test_broker_cancels_unfilled_and_returns_partial(fake, monkeypatch):
    m = fake("partial")
    from scalper.moomoo_broker import MoomooBroker
    monkeypatch.setenv("MOOMOO_TRADE_PASSWORD", "x")
    b = MoomooBroker(_cfg())
    f = b.execute("JP.7203", Side.BUY, 200, 1000, datetime.now())
    ctx = m.OpenSecTradeContext.instances[-1]
    assert ctx.cancelled == ["1"] and f.qty == 100


def test_broker_no_fill_returns_none(fake, monkeypatch):
    fake("none")
    from scalper.moomoo_broker import MoomooBroker
    monkeypatch.setenv("MOOMOO_TRADE_PASSWORD", "x")
    assert MoomooBroker(_cfg()).execute("JP.7203", Side.BUY, 100, 1000, datetime.now()) is None


def test_broker_positions(fake, monkeypatch):
    fake()
    from scalper.moomoo_broker import MoomooBroker
    monkeypatch.setenv("MOOMOO_TRADE_PASSWORD", "x")
    assert MoomooBroker(_cfg()).positions() == {"JP.7203": 100}


def _row(t, o, h, l, c, v=1000, code="JP.7203"):
    return {"code": code, "time_key": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_live_runner_finalizes_bar_on_time_key_change():
    from scalper.live import LiveRunner
    cfg = _cfg("paper")
    runner = LiveRunner(cfg)
    strat = ScriptedStrategy({0: Signal.LONG})
    risk = RiskManager(cfg.risk)
    eng = SymbolEngine("JP.7203", cfg, strat, risk, SimBroker(cfg.execution, "JP"))
    runner.engines["JP.7203"] = eng
    # 同じ足の更新が 2 回 → まだ確定しない
    runner._on_kline("JP.7203", _row("2026-01-05 10:01:00", 1000, 1001, 999, 1000))
    runner._on_kline("JP.7203", _row("2026-01-05 10:01:00", 1000, 1003, 999, 1002))
    assert strat.i == -1
    # 次の足が来たら直前の最終版で確定
    runner._on_kline("JP.7203", _row("2026-01-05 10:02:00", 1002, 1002, 1002, 1002))
    assert strat.i == 0 and strat.last_bar.high == 1003
    assert eng.position is not None and eng.position.entry_price == 1002
    # 古い足の再送は無視
    runner._on_kline("JP.7203", _row("2026-01-05 10:01:00", 1, 1, 1, 1))
    runner._on_kline("JP.7203", _row("2026-01-05 10:02:00", 1, 1, 1, 1))
    assert strat.i == 0


def test_live_runner_drain_uses_latest_quote_only():
    from scalper.live import LiveRunner
    cfg = _cfg("paper")
    runner = LiveRunner(cfg)
    eng = SymbolEngine("JP.7203", cfg, ScriptedStrategy({0: Signal.LONG}), RiskManager(cfg.risk),
                       SimBroker(cfg.execution, "JP"))
    runner.engines["JP.7203"] = eng
    runner._on_kline("JP.7203", _row("2026-01-05 10:01:00", 1000, 1001, 999, 1000))
    runner._on_kline("JP.7203", _row("2026-01-05 10:02:00", 1000, 1000, 1000, 1000))
    assert eng.position is not None
    runner.now = lambda: datetime(2026, 1, 5, 10, 2, 30)
    # 一時的に損切り価格を割ったが、最新価格は戻っている → 決済しない
    for p in (985, 1001):
        runner.events.put(("quote", "JP.7203", p))
    runner._drain(timeout=0.1)
    assert eng.position is not None
    runner.events.put(("quote", "JP.7203", 985))
    runner._drain(timeout=0.1)
    assert eng.position is None and eng.trades[0].reason == "stop"


def test_paper_broker_fills_at_book():
    from scalper.live import PaperBroker
    cfg = _cfg("paper")
    pb = PaperBroker(cfg, lambda c: (998.0, 1000.0))
    assert pb.execute("JP.7203", Side.BUY, 100, 999, datetime.now()).price == 1000
    assert pb.execute("JP.7203", Side.SELL, 100, 999, datetime.now()).price == 998
