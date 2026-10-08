from datetime import datetime, time

import pytest

from scalper.market import TradingSessions, round_to_tick, tick_size


def test_us_tick_size():
    assert tick_size(150.0) == 0.01
    assert tick_size(0.5) == 0.0001
    assert tick_size(150.0, "US", 0.05) == 0.05


def test_round_to_tick():
    assert round_to_tick(100.004, 0.01, "nearest") == 100.0
    assert round_to_tick(100.001, 0.01, "up") == 100.01
    assert round_to_tick(100.019, 0.01, "down") == 100.01
    assert round_to_tick(3001.3, 5, "up") == 3005
    # 浮動小数点誤差で 1 ティック上がらないこと
    assert round_to_tick(0.1 + 0.2, 0.1, "up") == pytest.approx(0.3)


def _rth():
    return TradingSessions([(time(9, 30), time(16, 0))], no_entry_first_minutes=5, no_entry_last_minutes=15,
                           flatten_before_close_minutes=5)


def test_rth_entry_window_and_flatten():
    s = _rth()
    d = datetime(2026, 1, 5)
    assert not s.can_enter(d.replace(hour=9, minute=33))      # 寄り直後
    assert s.can_enter(d.replace(hour=10))
    assert not s.can_enter(d.replace(hour=15, minute=50))     # 引け 15 分前以降
    assert not s.must_flatten(d.replace(hour=15, minute=54))
    assert s.must_flatten(d.replace(hour=15, minute=55))      # 引け 5 分前
    assert s.must_flatten(d.replace(hour=17))                 # 時間外
    assert not s.can_enter(d.replace(hour=8))


def test_flatten_each_session_toggle():
    s = TradingSessions([(time(4, 0), time(9, 30)), (time(9, 30), time(16, 0))], flatten_before_close_minutes=3,
                        flatten_each_session=True)
    t = datetime(2026, 1, 5, 9, 28)        # プレマーケット終了 2 分前
    assert s.must_flatten(t)
    s.flatten_each_session = False
    assert not s.must_flatten(t)


def _us_ext():
    return TradingSessions([(time(20, 0), time(4, 0)), (time(4, 0), time(9, 30)), (time(9, 30), time(16, 0)),
                            (time(16, 0), time(20, 0))], no_entry_first_minutes=5, no_entry_last_minutes=10,
                           flatten_before_close_minutes=3, flatten_each_session=True, day_rollover=time(20, 0))


def test_overnight_session_crosses_midnight():
    s = _us_ext()
    assert s.session_index(datetime(2026, 10, 8, 0, 35)) == 0     # 深夜 0:35 はオーバーナイト
    assert s.session_index(datetime(2026, 10, 7, 21, 0)) == 0
    assert s.can_enter(datetime(2026, 10, 8, 0, 35))
    assert not s.can_enter(datetime(2026, 10, 7, 20, 3))         # 開始直後
    assert s.must_flatten(datetime(2026, 10, 8, 3, 58))           # オーバーナイト終了 3 分前
    assert s.session_index(datetime(2026, 10, 8, 5, 0)) == 1      # プレマーケット
    assert s.must_flatten(datetime(2026, 10, 8, 19, 58))          # アフター終了 = 取引日の最後


def test_trading_day_rollover():
    s = _us_ext()
    assert s.trading_day(datetime(2026, 10, 7, 21, 0)) == s.trading_day(datetime(2026, 10, 8, 19, 0))
    assert s.trading_day(datetime(2026, 10, 8, 19, 59)) != s.trading_day(datetime(2026, 10, 8, 20, 1))
