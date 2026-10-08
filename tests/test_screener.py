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
    r = {"code": "CC.X", "last_price": 100, "high_price": 103, "low_price": 100, "prev_close_price": 100,
         "turnover": 5e8}
    out = rank([r], cfg)[0]
    assert out["amplitude"] == pytest.approx(3.0) and out["spread_pct"] is None and out["excluded"] is None


def test_universe_codes_match_market():
    for m, codes in UNIVERSE.items():
        assert all(c.startswith(m + ".") for c in codes)
        assert len(codes) == len(set(codes))


def test_runner_auto_selects_symbols(monkeypatch):
    m = fake_moomoo.build()
    monkeypatch.setitem(sys.modules, "moomoo", m)
    from scalper.live import LiveRunner
    from scalper.screener import select
    cfg = _cfg()
    cfg.auto_symbols.count = 3
    ctx = m.OpenQuoteContext()
    chosen = select(cfg, ctx, m)
    assert len(chosen) == 3 and all(c in UNIVERSE["US"] for c in chosen)
    # LiveRunner.run の流れ (選定 → エンジン作成)
    runner = LiveRunner(cfg)
    runner._connect()
    cfg.symbols = select(cfg, runner.quote_ctx, m)
    runner._build_engines()
    assert sorted(runner.engines) == sorted(chosen)
