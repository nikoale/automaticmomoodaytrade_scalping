from datetime import datetime, timedelta

from scalper.config import Config
from scalper.models import Bar, Signal
from scalper.strategies import Strategy


class FixedATR:
    def __init__(self, v):
        self.value = v


class ScriptedStrategy(Strategy):
    """指定した足番号でシグナルを出すテスト用戦略 (ATR は固定値)。"""
    name = "scripted"

    def __init__(self, signals: dict[int, Signal], atr: float = 10.0, stop_hint=None, target_hint=None):
        super().__init__()
        self.signals = signals
        self.atr = FixedATR(atr)
        self.i = -1
        self._sh, self._th = stop_hint, target_hint

    def on_bar(self, bar):
        self.i += 1
        self.last_bar = bar
        return self.signals.get(self.i, Signal.NONE)

    def stop_hint(self):
        return self._sh

    def target_hint(self):
        return self._th


def make_cfg(**risk) -> Config:
    cfg = Config(mode="backtest")
    cfg.session.no_entry_first_minutes = 0
    cfg.session.no_entry_last_minutes = 0
    cfg.session.flatten_before_close_minutes = 5
    cfg.execution.slippage_ticks = 0
    cfg.execution.tick_size = 1
    cfg.exits.stop_atr = 1.0
    cfg.exits.target_atr = 2.0
    cfg.exits.breakeven_atr = 0
    cfg.exits.max_hold_bars = 100
    cfg.exits.min_atr_ticks = 0
    cfg.risk.account_size = 1_000_000
    cfg.risk.risk_per_trade = 0.001     # 1000 USD
    cfg.risk.max_position_value = 10_000_000
    cfg.risk.lot_size = 100
    cfg.risk.max_daily_loss = 1e9
    for k, v in risk.items():
        setattr(cfg.risk, k, v)
    return cfg


def bars(prices, start=datetime(2026, 1, 5, 10, 0), spread=1.0):
    """(open, high, low, close) か close のみのリストから 1 分足を作る。"""
    out = []
    t = start
    for p in prices:
        if isinstance(p, tuple):
            o, h, l, c = p
        else:
            o = h = l = c = p
            h, l = p + spread, p - spread
        out.append(Bar(t, o, h, l, c, 1000))
        t += timedelta(minutes=1)
    return out
