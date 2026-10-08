from datetime import datetime

from scalper.config import RiskConfig
from scalper.risk import RiskManager


def test_position_size_respects_risk_value_and_lot():
    r = RiskManager(RiskConfig(account_size=1_000_000, risk_per_trade=0.003, max_position_value=500_000,
                               lot_size=100, max_daily_loss=10_000))
    # 許容損失 3000 円 / 損切り幅 10 円 = 300 株
    assert r.position_size(1000, 10) == 300
    # 建玉上限 50 万円 / 3000 円 = 166 → 100 株
    assert r.position_size(3000, 1) == 100
    # 単位未満は 0
    assert r.position_size(3000, 50) == 0


def test_daily_loss_limit_halts_and_resets_next_day():
    r = RiskManager(RiskConfig(max_daily_loss=1000, max_consecutive_losses=99))
    r.roll_day(datetime(2026, 1, 5, 9, 10))
    r.on_open("A")
    r.on_close("A", -1200)
    ok, why = r.can_open("A")
    assert not ok and "daily loss" in why
    r.roll_day(datetime(2026, 1, 6, 9, 10))
    assert r.can_open("A")[0]


def test_consecutive_losses_and_cooldown():
    r = RiskManager(RiskConfig(max_daily_loss=1e9, max_consecutive_losses=2, cooldown_bars_after_loss=2))
    r.roll_day(datetime(2026, 1, 5, 9, 10))
    r.on_open("A"); r.on_close("A", -1)
    assert r.can_open("A") == (False, "cooldown after loss")
    r.on_bar("A"); r.on_bar("A")
    assert r.can_open("A")[0]
    r.on_open("A"); r.on_close("A", -1)
    assert "consecutive" in r.can_open("B")[1]


def test_size_shrinks_near_daily_limit():
    r = RiskManager(RiskConfig(account_size=1_000_000, risk_per_trade=0.01, max_position_value=1e9,
                               lot_size=1, max_daily_loss=5_000, max_consecutive_losses=99))
    r.roll_day(datetime(2026, 1, 5))
    r.on_open("A"); r.on_close("A", -4_000)
    # 残り許容 1000 円 / 10 円 = 100 株
    assert r.position_size(1000, 10) == 100


def test_fractional_lot_for_crypto():
    r = RiskManager(RiskConfig(account_size=10_000, risk_per_trade=0.002, max_position_value=2_000,
                               lot_size=0.0001, max_daily_loss=60))
    q = r.position_size(60_000, 100)   # 20 ドル / 100 = 0.2, 上限 2000/60000 = 0.0333
    assert q == 0.0333
