from datetime import datetime

from helpers import ScriptedStrategy, bars, make_cfg

from scalper.broker import SimBroker
from scalper.engine import SymbolEngine
from scalper.models import Signal
from scalper.risk import RiskManager


def _engine(signals, cfg=None, **kw):
    cfg = cfg or make_cfg()
    risk = RiskManager(cfg.risk)
    eng = SymbolEngine("JP.7203", cfg, ScriptedStrategy(signals, **kw), risk, SimBroker(cfg.execution, "JP"))
    return eng, risk


def test_long_hits_target():
    eng, risk = _engine({0: Signal.LONG})
    # 1000 円リスク / ATR10 → 100 株, stop=990 target=1020
    for b in bars([1000, (1000, 1021, 999, 1015)]):
        eng.on_bar(b)
    assert len(eng.trades) == 1
    t = eng.trades[0]
    assert t.reason == "target" and t.qty == 100 and t.exit_price == 1020 and t.pnl == 2000
    assert eng.position is None and risk.trades_today == 1


def test_stop_has_priority_when_both_touched_and_gap_fills_at_open():
    eng, _ = _engine({0: Signal.LONG})
    for b in bars([1000, (985, 1030, 980, 1000)]):   # 窓を開けて損切りを下回って寄った
        eng.on_bar(b)
    t = eng.trades[0]
    assert t.reason == "stop" and t.exit_price == 985 and t.pnl == -1500


def test_short_disabled_by_default():
    eng, _ = _engine({0: Signal.SHORT})
    for b in bars([1000, 1000]):
        eng.on_bar(b)
    assert eng.position is None and not eng.trades


def test_short_allowed():
    cfg = make_cfg(allow_short=True)
    eng, _ = _engine({0: Signal.SHORT}, cfg)
    for b in bars([1000, (1000, 1001, 979, 985)]):
        eng.on_bar(b)
    t = eng.trades[0]
    assert t.direction == "SHORT" and t.exit_price == 980 and t.pnl == 2000


def test_time_stop_and_signal_exit():
    cfg = make_cfg()
    cfg.exits.max_hold_bars = 2
    eng, _ = _engine({0: Signal.LONG}, cfg)
    for b in bars([1000, 1001, 1002, 1003]):
        eng.on_bar(b)
    assert eng.trades[0].reason == "time_stop"

    eng, _ = _engine({0: Signal.LONG, 2: Signal.EXIT})
    for b in bars([1000, 1001, 1002]):
        eng.on_bar(b)
    assert eng.trades[0].reason == "signal"


def test_flatten_before_close_and_no_entry_outside_session():
    eng, _ = _engine({0: Signal.LONG, 3: Signal.LONG})
    seq = bars([1000, 1001, 1002], start=datetime(2026, 1, 5, 15, 23))  # 15:23, 15:24, 15:25(=引け5分前)
    for b in seq:
        eng.on_bar(b)
    assert eng.trades[-1].reason == "session_end"
    eng.on_bar(bars([1000], start=datetime(2026, 1, 5, 15, 40))[0])
    assert eng.position is None


def test_trailing_stop_ratchets():
    cfg = make_cfg()
    cfg.exits.trail_atr = 1.0
    cfg.exits.target_atr = 100
    eng, _ = _engine({0: Signal.LONG}, cfg)
    for b in bars([1000, (1000, 1050, 1000, 1045)]):
        eng.on_bar(b)
    assert eng.position.stop == 1040   # 最高値 1050 - ATR10
    eng.on_bar(bars([(1045, 1046, 1039, 1040)], start=datetime(2026, 1, 5, 9, 32))[0])
    assert eng.trades[0].reason == "stop" and eng.trades[0].pnl == 4000


def test_breakeven_stop():
    cfg = make_cfg()
    cfg.exits.breakeven_atr = 0.5
    eng, _ = _engine({0: Signal.LONG}, cfg)
    for b in bars([1000, (1000, 1006, 1000, 1004)]):
        eng.on_bar(b)
    assert eng.position.stop == 1001


def test_stop_and_target_hints():
    eng, _ = _engine({0: Signal.LONG}, stop_hint=995, target_hint=1008)
    eng.on_bar(bars([1000])[0])
    assert eng.position.stop == 995 and eng.position.target == 1008


def test_check_price_tick_exit():
    eng, _ = _engine({0: Signal.LONG})
    eng.on_bar(bars([1000])[0])
    eng.check_price(989, datetime(2026, 1, 5, 9, 30, 30))
    assert eng.trades[0].reason == "stop"


def test_min_atr_filter_blocks_entry():
    cfg = make_cfg()
    cfg.exits.min_atr_ticks = 20
    eng, _ = _engine({0: Signal.LONG}, cfg)
    eng.on_bar(bars([1000])[0])
    assert eng.position is None


class _FailingBroker(SimBroker):
    def __init__(self, *a):
        super().__init__(*a)
        self.fail_exits = True
        self.calls = 0

    def execute(self, code, side, qty, ref_price, t, reason="", exact=False):
        self.calls += 1
        if reason.startswith("entry"):
            return super().execute(code, side, qty, ref_price, t, reason, exact)
        return None if self.fail_exits else super().execute(code, side, qty, ref_price, t, reason, exact)


def test_failed_exit_is_throttled():
    cfg = make_cfg()
    risk = RiskManager(cfg.risk)
    broker = _FailingBroker(cfg.execution, "JP")
    eng = SymbolEngine("X", cfg, ScriptedStrategy({0: Signal.LONG}), risk, broker)
    eng.on_bar(bars([1000])[0])
    t = datetime(2026, 1, 5, 9, 31)
    eng.check_price(980, t)
    eng.check_price(980, t.replace(second=5))
    assert broker.calls == 2 and eng.position is not None    # entry + 1 exit 試行のみ
    broker.fail_exits = False
    eng.check_price(980, t.replace(second=11))
    assert eng.position is None


def test_vwap_and_risk_do_not_reset_at_midnight_with_rollover():
    from datetime import timedelta

    from scalper.config import load_config
    from scalper.models import Bar
    from scalper.strategies import EmaVwapMomentum
    cfg = load_config("config/config.us.ext.example.yaml")
    strat = EmaVwapMomentum()
    risk = RiskManager(cfg.risk)
    eng = SymbolEngine("US.NVDA", cfg, strat, risk, SimBroker(cfg.execution, "US"))
    t = datetime(2026, 10, 7, 23, 58)
    for i, px in enumerate([100, 200, 300]):          # 23:58, 23:59, 00:00 (日付またぎ)
        eng.on_bar(Bar(t + timedelta(minutes=i), px, px, px, px, 100))
    assert strat.vwap.value == 200                    # 0 時でリセットされていない
    eng.on_bar(Bar(datetime(2026, 10, 8, 20, 1), 50, 50, 50, 50, 100))
    assert strat.vwap.value == 50                     # 20:00 で新しい取引日
