import pytest

from scalper.backtest import run_backtest
from scalper.config import load_config
from scalper.data import generate_sample, load_csv, resample, save_csv
from scalper.strategies import STRATEGIES


def test_all_strategies_run_and_never_hold_overnight():
    data = generate_sample(days=10, seed=1)
    for name in STRATEGIES:
        cfg = load_config("config/config.us.example.yaml", {"mode": "backtest", "strategy": {"name": name}})
        res = run_backtest(cfg, {"US.AAPL": data})
        for t in res.trades:
            assert t.entry_time.date() == t.exit_time.date()
            assert float(t.qty).is_integer() and t.qty > 0
        assert res.stats()["trades"] == len(res.trades)


def test_csv_roundtrip_and_resample(tmp_path):
    data = generate_sample(days=1, seed=3)
    p = tmp_path / "x.csv"
    save_csv(data, p)
    loaded = load_csv(p)
    assert len(loaded) == len(data) and loaded[0].time == data[0].time
    five = resample(loaded, 5)
    # 9:31〜9:35 の 5 本が 9:35 の足にまとまる
    assert five[0].time.minute == 35 and five[0].high == max(b.high for b in loaded[:5])
    assert sum(b.volume for b in five) == sum(b.volume for b in loaded)


def test_example_configs_load():
    assert load_config("config/config.us.example.yaml").market == "US"
    assert load_config("config/config.us.ext.example.yaml").session.us_session == "ALL"


def test_non_us_rejected():
    with pytest.raises(ValueError, match="米国株"):
        load_config(overrides={"moomoo": {"trd_market": "JP"}})
    with pytest.raises(ValueError, match="米国株"):
        load_config(overrides={"symbols": ["JP.7203"]})
