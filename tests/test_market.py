from datetime import datetime, time

import pytest

from scalper.market import TradingSessions, round_to_tick, tick_size


@pytest.mark.parametrize("price,setting,expected", [
    (2999, "auto", 1), (3001, "auto", 5), (25000, "auto", 10),
    (2999, "topix500", 0.5), (800, "topix500", 0.1), (5000, "topix500", 1),
    (100, 0.25, 0.25),
])
def test_jp_tick_size(price, setting, expected):
    assert tick_size(price, "JP", setting) == expected


def test_us_tick_size():
    assert tick_size(150.0, "US") == 0.01
    assert tick_size(0.5, "US") == 0.0001


def test_round_to_tick():
    assert round_to_tick(3001.3, 5, "up") == 3005
    assert round_to_tick(3001.3, 5, "down") == 3000
    assert round_to_tick(100.004, 0.01, "nearest") == 100.0
    # 浮動小数点誤差で 1 ティック上がらないこと
    assert round_to_tick(0.1 + 0.2, 0.1, "up") == pytest.approx(0.3)


def _jp():
    return TradingSessions([(time(9, 0), time(11, 30)), (time(12, 30), time(15, 30))],
                           no_entry_first_minutes=5, no_entry_last_minutes=15,
                           flatten_before_close_minutes=5, flatten_at_lunch=True)


def test_sessions_entry_window():
    s = _jp()
    d = datetime(2026, 1, 5)
    assert not s.can_enter(d.replace(hour=9, minute=3))      # 寄り直後
    assert s.can_enter(d.replace(hour=9, minute=10))
    assert not s.can_enter(d.replace(hour=11, minute=20))    # 前場引け 15 分前
    assert not s.can_enter(d.replace(hour=12, minute=0))     # 昼休み
    assert s.can_enter(d.replace(hour=13, minute=0))
    assert not s.can_enter(d.replace(hour=15, minute=20))


def test_sessions_flatten():
    s = _jp()
    d = datetime(2026, 1, 5)
    assert not s.must_flatten(d.replace(hour=10))
    assert s.must_flatten(d.replace(hour=11, minute=26))
    assert s.must_flatten(d.replace(hour=15, minute=25))
    assert s.must_flatten(d.replace(hour=16))
    s.flatten_at_lunch = False
    assert not s.must_flatten(d.replace(hour=11, minute=26))
