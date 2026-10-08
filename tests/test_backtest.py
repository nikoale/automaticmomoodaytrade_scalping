from scalper.backtest import run_backtest
from scalper.config import load_config
from scalper.data import generate_sample, load_csv, resample, save_csv
from scalper.strategies import STRATEGIES


def test_all_strategies_run_and_never_hold_overnight():
    data = generate_sample(days=10, seed=1)
    for name in STRATEGIES:
        cfg = load_config(overrides={"mode": "backtest", "strategy": {"name": name}})
        res = run_backtest(cfg, {"JP.7203": data})
        for t in res.trades:
            assert t.entry_time.date() == t.exit_time.date()
            assert t.qty % 100 == 0
        st = res.stats()
        assert st["trades"] == len(res.trades)


def test_csv_roundtrip_and_resample(tmp_path):
    data = generate_sample(days=1, seed=3)
    p = tmp_path / "x.csv"
    save_csv(data, p)
    loaded = load_csv(p)
    assert len(loaded) == len(data) and loaded[0].time == data[0].time
    five = resample(loaded, 5)
    assert five[0].time.minute == 5 and five[0].high == max(b.high for b in loaded[:5])
    assert sum(b.volume for b in five) == sum(b.volume for b in loaded)


def test_example_configs_load():
    assert load_config("config/config.example.yaml").market == "JP"
    assert load_config("config/config.us.example.yaml").market == "US"


def test_simulate_rejected_for_jp():
    import pytest
    with pytest.raises(ValueError, match="SIMULATE"):
        load_config(overrides={"mode": "simulate"})


def test_crypto_config_paper_only_and_backtests():
    import pytest
    cfg = load_config("config/config.crypto.example.yaml", {"mode": "backtest"})
    data = generate_sample(days=3, start_price=60_000, market="US", seed=5)
    res = run_backtest(cfg, {"CC.BTCUSD": data})
    for t in res.trades:
        assert t.qty < 1                       # 小数数量
    with pytest.raises(ValueError, match="paper"):
        load_config("config/config.crypto.example.yaml", {"mode": "live"})
