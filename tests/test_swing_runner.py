"""フェーズ 4: 自動実行の予定 (日本時間・夏時間/冬時間・祝日) と、同じ分を 2 回動かさないこと。"""
from datetime import datetime

import pytest

from swing import calendar_us, runner
from swing_helpers import cfg as make_cfg

J = calendar_us.TOKYO


def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=J)


@pytest.fixture
def c(tmp_path):
    return make_cfg(data={"dir": str(tmp_path)})


def names(c, now, done=None):
    return [n for n, _ in runner.due(c, now, done or {})]


def test_close_run_after_us_close(c):
    # 金曜 10/9 の引けは日本時間 10/10 5:00 (夏時間) → 7:30 に引け後の処理
    assert runner.close_run_time(c, calendar_us.last_completed_session(at(2026, 10, 10, 8, 0))) == at(2026, 10, 10, 7, 30)
    assert "trade_close" not in names(c, at(2026, 10, 10, 7, 0), {"trade_close": "2026-10-08"})
    assert ("trade_close", "2026-10-09") in runner.due(c, at(2026, 10, 10, 7, 31), {})
    assert "trade_close" not in names(c, at(2026, 10, 10, 7, 31), {"trade_close": "2026-10-09"})
    # 冬時間 (12/1 の引けは 12/2 6:00 JST) でも 7:30
    assert runner.close_run_time(c, datetime(2026, 12, 1).date()) == at(2026, 12, 2, 7, 30)


def test_open_run_dst_and_winter_and_holiday(c):
    assert ("trade_open", "2026-10-12") in runner.due(c, at(2026, 10, 12, 22, 34), {"trade_close": "2026-10-09"})
    assert "trade_open" not in names(c, at(2026, 10, 12, 22, 31))           # 寄り付き + 3 分より前
    assert "trade_open" not in names(c, at(2026, 10, 12, 23, 40))           # 1 時間以上遅れたらやらない
    assert "trade_open" in names(c, at(2026, 12, 1, 23, 35))                # 冬時間は 23:30 寄り付き
    assert "trade_open" not in names(c, at(2026, 12, 1, 22, 35))
    assert "trade_open" not in names(c, at(2026, 11, 26, 23, 35))           # 感謝祭 (休場)


def test_weekly_screen_saturday_with_catch_up(c):
    assert "screen" in names(c, at(2026, 10, 10, 9, 1))
    assert "screen" not in names(c, at(2026, 10, 10, 8, 59), {"screen": "2026-10-03"})
    assert "screen" in names(c, at(2026, 10, 11, 20, 0), {"screen": "2026-10-03"})   # 日曜に起きても実行
    assert "screen" not in names(c, at(2026, 10, 12, 10, 0), {"screen": "2026-10-03"})  # 月曜はもう遅い
    assert "screen" not in names(c, at(2026, 10, 10, 9, 1), {"screen": "2026-10-10"})


def test_scheduler_runs_once_retries_and_waits_when_busy(c):
    now = {"t": at(2026, 10, 10, 7, 31)}
    calls, results = [], {"trade_close": ["error", "ok"], "screen": ["busy", "ok"]}

    def run(name):
        calls.append(name)
        return results[name].pop(0)
    s = runner.Scheduler(lambda: c, run, now=lambda: now["t"])
    assert s.tick() == []                                     # close は失敗 → 5 分後、screen はまだ時刻前
    assert s.tick() == []                                     # 5 分たっていない → 何もしない
    now["t"] = at(2026, 10, 10, 7, 37)
    assert s.tick() == ["trade_close"]
    assert s.tick() == [] and calls == ["trade_close", "trade_close"]
    now["t"] = at(2026, 10, 10, 9, 5)
    assert s.tick() == []                                     # busy → もう一度
    assert s.tick() == ["screen"]
    assert s.done()["trade_close"] == "2026-10-09" and s.done()["screen"] == "2026-10-10"
    s2 = runner.Scheduler(lambda: c, run, now=lambda: now["t"])   # 再起動しても同じ分は動かさない
    assert s2.tick() == []


def test_gives_up_after_retry_count(c):
    s = runner.Scheduler(lambda: c, lambda n: "error", now=lambda: t[0])
    t = [at(2026, 10, 10, 7, 31)]
    for k in range(3):
        s.tick()
        t[0] = t[0].replace(minute=t[0].minute + 6)
    assert s.done().get("trade_close") == "2026-10-09"


def test_upcoming_lists_three(c):
    up = runner.upcoming(c, at(2026, 10, 10, 10, 0))
    assert [x["name"] for x in up] == ["trade_close", "trade_open", "screen"] or len(up) == 3
    assert any(x["name"] == "trade_open" and x["time"].startswith("10/12") for x in up)
