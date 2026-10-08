from datetime import date

import pytest

from scalper.indicators import ATR, EMA, RSI, RollingMean, SessionVWAP


def test_ema_seeds_with_sma_then_smooths():
    e = EMA(3)
    assert e.update(1) is None
    assert e.update(2) is None
    assert e.update(3) == pytest.approx(2.0)
    assert e.update(4) == pytest.approx(2.0 + 0.5 * (4 - 2.0))


def test_rsi_extremes():
    r = RSI(3)
    for x in [1, 2, 3, 4, 5]:
        v = r.update(x)
    assert v == 100.0
    r = RSI(3)
    for x in [5, 4, 3, 2, 1]:
        v = r.update(x)
    assert v == pytest.approx(0.0)


def test_atr_uses_true_range_with_gaps():
    a = ATR(2)
    a.update(10, 9, 9.5)          # TR = 1
    v = a.update(12, 11, 11.5)    # TR = max(1, |12-9.5|, |11-9.5|) = 2.5
    assert v == pytest.approx((1 + 2.5) / 2)


def test_vwap_resets_daily():
    v = SessionVWAP()
    v.update(date(2026, 1, 5), 10, 10, 10, 100)
    assert v.update(date(2026, 1, 5), 20, 20, 20, 100) == pytest.approx(15)
    assert v.update(date(2026, 1, 6), 30, 30, 30, 100) == pytest.approx(30)


def test_rolling_mean():
    m = RollingMean(2)
    assert m.update(1) is None
    assert m.update(3) == 2
    assert m.update(5) == 4
