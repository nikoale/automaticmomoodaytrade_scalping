from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from swing import backtest, indicators, risk
from swing.screener import next_earnings, screen_at
from swing_helpers import cfg, make_panel, trend_with_breakout

N = 320
DATES = pd.bdate_range("2019-01-01", periods=N)


def _up(start, end, vol=1_000_000.0):
    return {"close": np.linspace(start, end, N), "volume": np.full(N, vol)}


# ---------------------------------------------------------------- スクリーナー
def _screen_panel():
    s = {
        "GOOD1": _up(20, 60, vol=1.5e6),
        "GOOD2": _up(20, 55),
        "CHEAP": _up(5, 9.5),                     # 株価 10 ドル未満
        "PRICEY": _up(150, 300),                  # 100 ドル超
        "THIN": _up(20, 50, vol=200_000),         # 出来高不足
        "DOWN": _up(60, 20),                      # 下降トレンド
        "FLAT": {"close": np.full(N, 30.0), "volume": np.full(N, 1e6)},
    }
    # GOOD1 は直近 20 日だけ出来高を増やす (出来高の伸びで 1 位)
    s["GOOD1"]["volume"][-20:] = 4e6
    return make_panel(s, DATES)


def test_screener_filters_and_ranking():
    c = cfg()
    ind = indicators.compute_all(_screen_panel(), c)
    res = screen_at(ind, N - 1, c, earnings={})
    syms = [x["symbol"] for x in res.items]
    assert syms == ["GOOD1", "GOOD2"]                          # 出来高の伸び順
    it = res.items[0]
    assert it["high_20d"] > 0 and it["atr_14"] > 0 and it["earnings_unknown"] is True
    assert res.counts["株価"] < res.counts["データあり"]


def test_screener_market_cap_and_earnings():
    c = cfg(screener={"earnings_unknown_policy": "exclude"})
    ind = indicators.compute_all(_screen_panel(), c)
    d = DATES[-1].date()
    past = [date(2019, 5, 1), date(2019, 8, 1), date(2019, 11, 1), date(2020, 2, 1)]
    earn = {"GOOD1": past + [d + timedelta(days=10)],       # 15 営業日以内 → 除外
            "GOOD2": past + [d + timedelta(days=40)]}
    res = screen_at(ind, N - 1, c, earnings=earn)
    assert [x["symbol"] for x in res.items] == ["GOOD2"]
    res = screen_at(ind, N - 1, c, earnings={}, market_cap={"GOOD1": 1e9, "GOOD2": 1e8})
    assert res.items == [] and set(res.unknown_earnings) == {"GOOD1"}   # GOOD2 は時価総額で落ちる
    # 近似時価総額 (現在の発行済株数 × 株価)
    res = screen_at(ind, N - 1, cfg(), earnings={}, shares={"GOOD1": 1e8, "GOOD2": 1e6})
    assert [x["symbol"] for x in res.items] == ["GOOD1"]


def test_next_earnings_unknown_rules():
    ds = [date(2023, 1, 25), date(2023, 4, 25), date(2023, 7, 25)]
    assert next_earnings(ds, date(2023, 5, 1)) == (date(2023, 7, 25), True)
    assert next_earnings(ds, date(2023, 8, 1))[1] is False             # 予定が未登録
    assert next_earnings(ds, date(2022, 6, 1))[1] is False             # データが始まる前
    assert next_earnings(None, date(2023, 5, 1)) == (None, False)


def test_index_filter_empties_watchlist():
    c = cfg()
    p = _screen_panel()
    p["bench_close"] = pd.Series(np.linspace(400, 300, N), index=DATES)   # 指数が下落 → 200 日線割れ
    ind = indicators.compute_all(p, c)
    res = screen_at(ind, N - 1, c, earnings={})
    assert not res.index_ok and res.items == [] and "新規停止" in res.note
    off = cfg(strategy={"index_filter": False})
    assert screen_at(indicators.compute_all(p, off), N - 1, off, earnings={}).items


# ---------------------------------------------------------------- バックテスト
def _bt(series, c=None, bench=None, **kw):
    c = c or cfg(backtest={"start": str(DATES[260].date())})
    ind = indicators.compute_all(make_panel(series, DATES, bench), c)
    return backtest.run(c, ind, **kw), c


def test_breakout_entry_next_open_with_slippage_and_fees():
    b = 280
    s = trend_with_breakout(N, b)
    res, c = _bt({"A": s})
    entries = [d for d in res.decisions if d["type"] == "entry"]
    assert entries, "ブレイク翌日にエントリーするはず"
    e = entries[0]
    assert e["date"] == str(DATES[b + 1].date())
    open_next = s["close"][b]                                  # helper: open = 前日終値
    assert e["price"] == pytest.approx(open_next * 1.001, rel=1e-4)
    t = res.trades[0]
    assert t.cost == pytest.approx(risk.buy_total(t.shares, t.entry_price, c))


def test_gap_down_stop_fills_at_open():
    b = 280
    s = trend_with_breakout(N, b)
    s["open"] = np.r_[s["close"][0], s["close"][:-1]].copy()
    # エントリー 2 日後に大きく窓を開けて下落
    s["close"][b + 3:] = s["close"][b] * 0.80
    s["open"][b + 3] = s["close"][b] * 0.82
    res, c = _bt({"A": s})
    t = res.trades[0]
    assert t.reason == "stop" and t.exit_date == DATES[b + 3].date()
    assert t.exit_price == pytest.approx(s["open"][b + 3] * 0.999, rel=1e-4)   # 逆指値ではなく寄り値で約定


def test_t_plus_1_blocks_rebuy_with_same_day_sale_proceeds():
    # 現金が 1 ポジション分しかない状況: A を売った同じ寄り付きで B は買えない
    b = 280
    a = trend_with_breakout(N, b)
    a["close"][b + 4:] = a["close"][b] * 0.995                 # 伸びない → 後で売られる
    bb = trend_with_breakout(N, b + 4)                          # A を売る日の前日に B がブレイク
    c = cfg(backtest={"start": str(DATES[260].date())}, risk={"max_position_pct": 100.0, "risk_per_trade_pct": 100.0},
            strategy={"max_positions": 3, "time_exit_days": 3})
    res, c = _bt({"A": a, "B": bb}, c=c)
    ent = [d for d in res.decisions if d["type"] in ("entry", "skip")]
    ex = [d for d in res.decisions if d["type"] == "exit" and d["symbol"] == "A"]
    assert ex and ex[0]["reason"] == "time_exit"
    # B のシグナルは A を売る前日の引け。翌寄り付きの A の売却代金は T+1 なので B の購入に使えない → 見送り
    b_events = [d for d in ent if d["symbol"] == "B"]
    assert b_events and b_events[0]["type"] == "skip"
    assert DATES.get_loc(pd.Timestamp(b_events[0]["date"])) + 1 == DATES.get_loc(pd.Timestamp(ex[0]["date"]))
    assert not [d for d in ent if d["symbol"] == "B" and d["type"] == "entry" and d["date"] == ex[0]["date"]]


def test_cooldown_and_max_positions():
    b = 280
    series = {f"S{i}": trend_with_breakout(N, b) for i in range(5)}
    res, c = _bt(series)
    first_day = [d for d in res.decisions if d["type"] == "entry" and d["date"] == str(DATES[b + 1].date())]
    assert len(first_day) == 3                                  # 最大 3 銘柄
    assert [d["rank"] for d in first_day] == sorted(d["rank"] for d in first_day)   # ランク上位から
    assert res.n_positions.max() <= 3
    # クールダウン: 同じ銘柄の 手仕舞い → 再エントリー は 6 営業日以上あく
    by_sym = {}
    for d in res.decisions:
        if d["type"] in ("entry", "exit"):
            by_sym.setdefault(d["symbol"], []).append((d["type"], DATES.get_loc(pd.Timestamp(d["date"]))))
    for evs in by_sym.values():
        for (k1, i1), (k2, i2) in zip(evs, evs[1:]):
            if k1 == "exit" and k2 == "entry":
                assert i2 - i1 >= 6


def test_index_filter_exits_all_next_open():
    b = 280
    bench = np.r_[np.linspace(300, 400, 290), np.linspace(400, 250, N - 290)]   # 290 日目から急落
    res, _ = _bt({"A": trend_with_breakout(N, b)}, bench=bench)
    ex = [t for t in res.trades if t.reason == "index_filter"]
    assert ex, "指数が 200 日線を割ったら手仕舞うはず"
    res2, _ = _bt({"A": trend_with_breakout(N, b)}, bench=bench, index_filter=False)
    assert not [t for t in res2.trades if t.reason == "index_filter"]


def test_earnings_exit_two_days_before():
    b = 280
    s = trend_with_breakout(N, b)
    e_day = DATES[b + 8].date()
    # ここで確かめたいのは手仕舞いのタイミングなので、スクリーナーの決算除外は短くして監視リストに入れる
    c = cfg(backtest={"start": str(DATES[260].date())}, screener={"earnings_exclude_days": 2})
    ind = indicators.compute_all(make_panel({"A": s}, DATES), c)
    earn = {"A": [date(2019, 4, 1), date(2019, 7, 1), date(2019, 10, 1), e_day]}
    res = backtest.run(c, ind, earnings=earn)
    ex = [t for t in res.trades if t.reason == "earnings"]
    assert ex, "決算前に手仕舞うはず"
    assert ex[0].exit_date == DATES[b + 6].date()               # 決算日 (b+8) の 2 営業日前の寄り付き


def test_stats_fields():
    res, _ = _bt({"A": trend_with_breakout(N, 280)})
    s = backtest.stats(res)
    for k in ("総損益_usd", "年率リターン_pct", "勝率_pct", "損益レシオ", "最大ドローダウン_pct", "年別損益_usd", "取引回数",
              "平均保有日数", "現金比率_平均_pct"):
        assert k in s
    assert s["最終資金_usd"] == pytest.approx(res.initial + sum(t.pnl for t in res.trades), rel=1e-6)
