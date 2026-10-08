import sys

import pytest

import fake_moomoo

from scalper.config import load_config
from scalper.screener import UNIVERSE, rank


def _cfg():
    cfg = load_config("config/config.us.example.yaml", {"mode": "paper"})
    cfg.auto_symbols.enabled = True
    return cfg


def _row(code, amp, turnover, last=100.0, bid=99.99, ask=100.01, vr=1.0):
    return {"code": code, "last_price": last, "amplitude": amp, "turnover": turnover, "bid_price": bid,
            "ask_price": ask, "volume_ratio": vr}


def test_rank_orders_by_score_and_excludes():
    cfg = _cfg()
    rows = [_row("US.A", 2.0, 2e8), _row("US.B", 4.0, 2e8), _row("US.C", 0.5, 5e9),      # 値幅不足
            _row("US.D", 5.0, 1e6), _row("US.E", 3.0, 2e8, bid=99.0, ask=101.0),          # 代金不足 / スプレッド
            _row("US.F", 3.0, 2e8, last=2.0), _row("US.G", 2.0, 2e8, vr=3.0)]             # 低位株 / 出来高急増
    out = rank(rows, cfg)
    ok = [r["code"] for r in out if r["excluded"] is None]
    assert ok == ["US.G", "US.B", "US.A"]
    reasons = {r["code"]: r["excluded"] for r in out}
    assert reasons["US.C"] == "値幅が小さい" and reasons["US.D"] == "売買代金が少ない"
    assert reasons["US.E"] == "スプレッドが広い" and "未満" in reasons["US.F"]


def test_amplitude_computed_when_missing_and_no_book_ok():
    cfg = _cfg()
    r = {"code": "US.X", "last_price": 100, "high_price": 103, "low_price": 100, "prev_close_price": 100,
         "turnover": 5e8}
    out = rank([r], cfg)[0]
    assert out["amplitude"] == pytest.approx(3.0) and out["spread_pct"] is None and out["excluded"] is None


def test_universe_is_us_and_unique():
    assert all(c.startswith("US.") for c in UNIVERSE)
    assert len(UNIVERSE) == len(set(UNIVERSE))


def test_runner_auto_selects_symbols(monkeypatch):
    m = fake_moomoo.build()
    monkeypatch.setitem(sys.modules, "moomoo", m)
    from scalper.live import LiveRunner
    from scalper.screener import select
    cfg = _cfg()
    cfg.auto_symbols.count = 3
    ctx = m.OpenQuoteContext()
    chosen = select(cfg, ctx, m)
    assert len(chosen) == 3 and all(c in UNIVERSE for c in chosen)
    # LiveRunner.run の流れ (選定 → エンジン作成)
    runner = LiveRunner(cfg)
    runner._connect()
    cfg.symbols = select(cfg, runner.quote_ctx, m)
    runner._build_engines()
    assert sorted(runner.engines) == sorted(chosen)


def _ranked(codes):
    return [{"code": c, "excluded": None} for c in codes]


def test_choose_keeps_current_if_still_near_top():
    from scalper.screener import choose
    ranked = _ranked(["A", "B", "C", "D", "E"])
    assert choose(ranked, [], 2) == ["A", "B"]
    assert choose(ranked, ["D"], 2, keep_factor=2.0) == ["D", "A"]      # D は上位 4 以内なので残す
    assert choose(ranked, ["E"], 2, keep_factor=2.0) == ["A", "B"]      # E は圏外なので入れ替え
    excluded = [{"code": "A", "excluded": "値幅が小さい"}] + _ranked(["B", "C"])
    assert choose(excluded, ["A"], 1) == ["B"]


def _auto_runner(monkeypatch, count=2):
    m = fake_moomoo.build()
    m.OpenQuoteContext.amp_override = {}
    monkeypatch.setitem(sys.modules, "moomoo", m)
    from scalper.live import LiveRunner
    from scalper.screener import select
    cfg = _cfg()
    cfg.auto_symbols.count = count
    cfg.auto_symbols.universe = ["US.A", "US.B", "US.C", "US.D", "US.E", "US.F"]
    runner = LiveRunner(cfg)
    runner._connect()
    m.OpenQuoteContext.amp_override = {"US.A": 4.0, "US.B": 3.9}
    cfg.symbols = select(cfg, runner.quote_ctx, m)
    runner._build_engines()
    runner._subscribe()
    for e in runner.engines.values():
        runner._warmup(e)
    return m, runner


def test_refresh_swaps_symbols_but_keeps_open_positions(monkeypatch):
    from datetime import datetime

    from scalper.models import Position
    m, runner = _auto_runner(monkeypatch)
    assert sorted(runner.engines) == ["US.A", "US.B"]
    eng_a = runner.engines["US.A"]
    eng_a.position = Position(qty=10, entry_price=100, entry_time=datetime(2026, 1, 5, 10), stop=99, target=102)
    eng_a.last_price = 100
    runner.risk.on_open("US.A")
    # 相場が変わって E, F が上位に。A, B は上位 4 (count × 2) の圏外
    m.OpenQuoteContext.amp_override = {"US.E": 5.0, "US.F": 4.8, "US.C": 4.6, "US.D": 4.5, "US.A": 1.6, "US.B": 1.6}
    runner.refresh_symbols()
    assert set(runner.engines) == {"US.A", "US.E", "US.F"}          # B は即削除、A は決済待ち
    assert runner._retiring == {"US.A"} and not runner.engines["US.A"].enabled
    assert "US.B" in runner.quote_ctx.unsubscribed
    assert runner.engines["US.E"].enabled and runner.engines["US.E"].strategy.ready
    assert runner.engines["US.A"].risk is runner.engines["US.E"].risk   # 損失上限は口座全体で共有
    # A を決済したら外れる
    runner.engines["US.A"].force_flatten(datetime(2026, 1, 5, 10, 5))
    runner._retire_flat()
    assert set(runner.engines) == {"US.E", "US.F"} and "US.A" in runner.quote_ctx.unsubscribed
    assert runner.snapshot()["chosen"] == ["US.E", "US.F"]


def test_paused_state_applies_to_new_symbols(monkeypatch):
    m, runner = _auto_runner(monkeypatch)
    runner._on_command("pause")
    m.OpenQuoteContext.amp_override = {"US.E": 5.0, "US.F": 4.8, "US.C": 4.6, "US.D": 4.5, "US.A": 1.6, "US.B": 1.6}
    runner.refresh_symbols()
    assert all(not e.enabled for e in runner.engines.values())
    runner._on_command("resume")
    assert all(e.enabled for e in runner.engines.values())


def test_refresh_schedule_session_start_and_closed_market(monkeypatch):
    from datetime import datetime
    m, runner = _auto_runner(monkeypatch)
    calls = []
    runner.refresh_symbols = lambda: calls.append(1)
    # 市場が閉まっている (時間外) → 予定時刻が来ても選び直さない
    runner._last_session_idx = None
    runner._next_refresh = 0
    runner._maybe_refresh(datetime(2026, 1, 5, 8, 0))
    assert calls == [] and runner._next_refresh is None
    # 寄り付き (9:30) 後 → 5 分後に予定
    runner._maybe_refresh(datetime(2026, 1, 5, 9, 31))
    assert runner._next_refresh is not None and calls == []
    runner._next_refresh = 0                      # 時間経過をシミュレート
    runner._maybe_refresh(datetime(2026, 1, 5, 9, 36))
    assert calls == [1] and runner._next_refresh is not None   # 次回 (30 分後) も予定される
