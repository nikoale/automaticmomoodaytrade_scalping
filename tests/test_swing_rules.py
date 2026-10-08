from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from swing import calendar_us as cal
from swing import indicators, risk, strategy
from swing.strategy import Position
from swing_helpers import cfg


# ---------------------------------------------------------------- カレンダー
@pytest.mark.parametrize("d", ["2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19",
                               "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25", "2023-01-02", "2022-06-20",
                               "2021-07-05", "2025-01-09"])
def test_nyse_holidays(d):
    assert not cal.is_trading_day(date.fromisoformat(d))


@pytest.mark.parametrize("d", ["2021-12-31",    # 2022/1/1 が土曜でも 12/31 は休場しない
                               "2021-06-18",    # Juneteenth は 2022 年から
                               "2024-11-29", "2024-07-05"])
def test_trading_days(d):
    assert cal.is_trading_day(date.fromisoformat(d))


def test_trading_day_counting():
    assert cal.next_trading_day(date(2024, 3, 28)) == date(2024, 4, 1)          # Good Friday をまたぐ
    assert cal.trading_days_between(date(2024, 3, 28), date(2024, 4, 3)) == 3   # 4/1, 4/2, 4/3
    assert cal.add_trading_days(date(2024, 12, 24), 1) == date(2024, 12, 26)


def test_dst_jst_session_times():
    o, c = cal.session_times_jst(date(2026, 7, 1))      # 夏時間
    assert (o.hour, o.minute, c.hour) == (22, 30, 5)
    o, c = cal.session_times_jst(date(2026, 1, 15))     # 冬時間
    assert (o.hour, o.minute, c.hour) == (23, 30, 6)
    # 日本時間 土曜 9:00 → 直近の完了セッションは金曜
    assert cal.last_completed_session(datetime(2026, 10, 10, 9, 0, tzinfo=cal.TOKYO)) == date(2026, 10, 9)
    # 日本時間 水曜 3:00 (米国 火曜の取引中) → 月曜
    assert cal.last_completed_session(datetime(2026, 10, 7, 3, 0, tzinfo=cal.TOKYO)) == date(2026, 10, 5)


# ---------------------------------------------------------------- 指標
def test_atr_and_breakout_levels_use_only_past():
    idx = pd.bdate_range("2024-01-01", periods=30)
    h = pd.DataFrame({"A": np.arange(30, dtype=float) + 11}, index=idx)
    lo = h - 2
    c = h - 1
    a = indicators.atr(h, lo, c, 14)
    assert a["A"].iloc[:13].isna().all() and a["A"].iloc[-1] == pytest.approx(2.0)   # TR は常に 2
    prev = indicators.rolling_max(h, 20).shift(1)
    assert prev["A"].iloc[25] == h["A"].iloc[24]                                     # 当日を含まない


# ---------------------------------------------------------------- 戦略ルール
def _pos(**kw):
    base = dict(symbol="A", shares=10, entry_date=date(2024, 1, 2), entry_idx=0, entry_price=50.0, entry_atr=1.0,
                stop=48.0, highest=50.0)
    base.update(kw)
    return Position(**base)


def test_entry_signal():
    c = cfg()
    assert strategy.entry_signal(51, 50, 1.5e6, 1e6, c)
    assert not strategy.entry_signal(51, 50, 1.4e6, 1e6, c)     # 出来高不足
    assert not strategy.entry_signal(50, 50, 3e6, 1e6, c)       # 上抜けていない (同値)
    assert not strategy.entry_signal(None, 50, 3e6, 1e6, c)


def test_trailing_starts_after_2atr_and_never_lowers():
    c = cfg()
    p = _pos()
    strategy.update_stop(p, close=51.5, high=51.8, sma_trail=49.0, atr_now=1.0, cfg=c)
    assert not p.trailing and p.stop == 48.0                    # 含み益 1.5 ATR → まだ
    strategy.update_stop(p, close=52.5, high=53.0, sma_trail=49.0, atr_now=1.0, cfg=c)
    assert p.trailing and p.stop == pytest.approx(51.0)         # max(SMA20 49, 高値 53 − 2) = 51
    strategy.update_stop(p, close=51.2, high=51.5, sma_trail=50.0, atr_now=1.5, cfg=c)
    assert p.stop == pytest.approx(51.0)                        # 候補 50 / 50 → 下げない
    strategy.update_stop(p, close=56, high=57, sma_trail=55.5, atr_now=1.0, cfg=c)
    assert p.stop == pytest.approx(55.5)                        # SMA20 の方が高い


def test_stop_fill_gap_and_intraday():
    assert strategy.stop_fill(47.0, 46.0, 48.0) == 47.0         # 寄りで下回る → 寄り値
    assert strategy.stop_fill(49.0, 47.5, 48.0) == 48.0         # 日中に触れる → 逆指値
    assert strategy.stop_fill(49.0, 48.5, 48.0) is None


def test_exit_reasons():
    c = cfg()
    p = _pos()
    assert strategy.exit_reason(p, 5, 51, None, index_ok=False, cfg=c) == "index_filter"
    # 決算まで 3 営業日 → 翌日 (= 決算 2 営業日前) に売る / 4 営業日ならまだ
    assert strategy.exit_reason(p, 5, 51, 3, True, c) == "earnings"
    assert strategy.exit_reason(p, 5, 51, 4, True, c) is None
    # 保有 20 日で 含み益 < 1 ATR → 時間切れ / 1 ATR 以上なら継続
    assert strategy.exit_reason(p, 20, 50.9, None, True, c) == "time_exit"
    assert strategy.exit_reason(p, 20, 51.0, None, True, c) is None
    assert strategy.exit_reason(p, 19, 50.0, None, True, c) is None


# ---------------------------------------------------------------- 資金管理
def test_position_size_rules():
    c = cfg()
    # 資金 1333 ドル: リスク 2% = 26.67 / (ATR 1.5 × 2) = 8.9 → 8 株。上限 40% = 533 / 50 = 10.7
    assert risk.position_size(1333.33, 50.0, 1.5, c) == 8
    # ATR が小さい → 40% 上限 (533 / 50 = 10)
    assert risk.position_size(1333.33, 50.0, 0.2, c) == 10
    # 現金が足りない → 減らす
    assert risk.position_size(1333.33, 50.0, 0.2, c, cash_available=200) == 3
    # 1 株未満 → 見送り
    assert risk.position_size(1333.33, 95.0, 30.0, c) == 0


def test_commission_and_cap():
    c = cfg()
    assert risk.commission(1000, c) == pytest.approx(1.32)
    assert risk.commission(100_000, c) == 22.0
    assert risk.buy_total(10, 50, c) == pytest.approx(500 + 0.66)
    assert risk.sell_net(10, 50, c) == pytest.approx(500 - 0.66)


def test_t_plus_1_ledger():
    led = risk.SettlementLedger(1000.0, 1)
    led.spend(400)
    led.add_sale(available_key=6, amount=300)      # 5 日目に売却 → 6 日目から使える
    assert led.available(5) == 600 and led.available(6) == 900
    with pytest.raises(ValueError):
        led.spend(700)                             # まだ受渡されていない分は使えない
    led.settle(6)
    led.spend(700)
    assert led.total == pytest.approx(200)


def test_weekly_loss_guard():
    c = cfg()
    g = risk.WeeklyLossGuard(c)
    g.start_week(date(2024, 1, 8), 1000)
    assert not g.update(date(2024, 1, 9), 950)     # −5%
    assert g.update(date(2024, 1, 10), 939)        # −6.1% → 今週は停止
    assert g.update(date(2024, 1, 11), 1000)       # 戻しても今週は停止のまま
    g.start_week(date(2024, 1, 15), 1000)
    assert not g.blocked(date(2024, 1, 15))        # 翌週は再開
