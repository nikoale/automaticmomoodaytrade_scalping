from datetime import date

import numpy as np
import pandas as pd
import pytest

from swing import data
from swing_helpers import cfg

NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N
ZZZT|Test Company - Common Stock|Q|Y|N|100|N|N
BADF|Bad Finance Inc. - Common Stock|Q|N|D|100|N|N
SPCX|Example Acquisition Corp - Class A Ordinary Shares|G|N|N|100|N|N
SPCXW|Example Acquisition Corp - Warrant|G|N|N|100|N|N
ADRX|Foreign Co - American Depositary Shares|Q|N|N|100|N|N
File Creation Time: 1007202618:00|||||||
"""
OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.B|Berkshire Hathaway Inc. Class B Common Stock|N|BRK.B|N|100|N|BRK.B
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
XYZ$A|XYZ Corp 6% Preferred Stock Series A|N|XYZpA|N|100|N|XYZ-A
NEWC|New Co Common Stock|A|NEWC|N|100|N|NEWC
File Creation Time: 1007202618:00|||||||
"""


def test_universe_keeps_only_common_stocks():
    df = data.parse_symbol_directory(NASDAQ, OTHER, cfg())
    assert list(df["symbol"]) == ["AAPL", "BRK.B", "NEWC"]
    assert data.yahoo_symbol("BRK.B") == "BRK-B"


def test_price_cache_roundtrip_and_panel(tmp_path):
    c = cfg(data={"dir": str(tmp_path)})
    idx = pd.bdate_range("2020-01-01", periods=300)
    def frame(close, vol=1e6):
        cl = np.asarray(close, dtype=float)
        return pd.DataFrame({"open": cl, "high": cl * 1.01, "low": cl * 0.99, "close": cl, "volume": vol}, index=idx)
    data.write_prices(c, "SPY", frame(np.linspace(300, 400, 300)))
    data.write_prices(c, "GOOD", frame(np.linspace(20, 50, 300)))
    data.write_prices(c, "PENNY", frame(np.linspace(1, 4, 300)))                 # 上昇率は GOOD より上だが 10 ドル未満
    short = frame(np.linspace(20, 30, 300)).iloc[-100:]                            # 200 日分ない → 除外
    data.write_prices(c, "SHORT", short)
    back = data.read_prices(c, "GOOD")
    assert len(back) == 300 and back["close"].iloc[-1] == pytest.approx(50)
    panel = data.load_panel(c, ["GOOD", "PENNY", "SHORT", "MISSING"])
    assert list(panel["close"].columns) == ["GOOD"]                                # PENNY は刈り込み
    assert panel["bench_close"].iloc[-1] == pytest.approx(400)
    # 6 ヶ月上昇率の順位は刈り込み前の全銘柄で計算している (PENNY の方が上 → GOOD は 2 銘柄中 2 位)
    assert panel["mom_pct"]["GOOD"].iloc[-1] == pytest.approx(0.5)               # GOOD と PENNY の 2 銘柄中
    # 差分更新の継ぎ足し
    new = frame(np.linspace(20, 50, 300)).iloc[-5:].copy()
    new.index = pd.bdate_range(idx[-1] + pd.Timedelta(days=1), periods=5)
    merged = data._merge(back, new)
    assert len(merged) == 305


def test_earnings_cache(tmp_path):
    c = cfg(data={"dir": str(tmp_path)})
    assert data.read_earnings(c, "AAA") is None
    data.write_earnings(c, "AAA", [date(2024, 1, 25), date(2024, 4, 25)])
    data.write_earnings(c, "AAA", [date(2024, 4, 25), date(2024, 7, 25)])        # 追記・重複排除
    assert data.read_earnings(c, "AAA") == [date(2024, 1, 25), date(2024, 4, 25), date(2024, 7, 25)]


def test_fetch_yahoo_parses_multiindex(monkeypatch):
    import sys
    import types
    idx = pd.DatetimeIndex(pd.bdate_range("2024-01-01", periods=3), tz="America/New_York")
    cols = pd.MultiIndex.from_product([["AAPL", "BRK-B"], ["Open", "High", "Low", "Close", "Volume"]])
    df = pd.DataFrame(np.arange(30, dtype=float).reshape(3, 10) + 1, index=idx, columns=cols)
    df[("BRK-B", "Close")] = np.nan                       # データなし → 除外
    calls = {}

    def download(tickers, **kw):
        calls.update(kw, tickers=tickers)
        return df
    fake = types.SimpleNamespace(download=download)
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    out = data.fetch_yahoo(["AAPL", "BRK.B"], "2024-01-01", pause=0)
    assert list(out) == ["AAPL"] and list(out["AAPL"].columns) == data.PRICE_COLS
    assert out["AAPL"].index.tz is None and calls["auto_adjust"] is True and "BRK-B" in calls["tickers"]


def test_fetch_earnings_yahoo_parses_tz_index(monkeypatch):
    import sys
    import types
    idx = pd.DatetimeIndex(["2024-04-25 16:00", "2024-07-25 16:00"], tz="America/New_York")
    tk = types.SimpleNamespace(get_earnings_dates=lambda limit: pd.DataFrame({"EPS Estimate": [1, 2]}, index=idx))
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=lambda s: tk))
    assert data.fetch_earnings_yahoo("AAA") == [date(2024, 4, 25), date(2024, 7, 25)]


def test_https_uses_certifi_bundle(monkeypatch):
    import certifi
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b"ok"

    def fake_urlopen(req, timeout=None, context=None):
        seen["ctx"] = context
        return Resp()
    monkeypatch.setattr(data.urllib.request, "urlopen", fake_urlopen)
    assert data._get("https://example.com/x") == b"ok"
    ctx = seen["ctx"]
    assert ctx is not None and ctx.verify_mode.name == "CERT_REQUIRED"      # 検証は省略しない
    assert ctx.cert_store_stats()["x509_ca"] > 0
    assert certifi.where()


def test_partial_cache_triggers_full_redownload(tmp_path, monkeypatch):
    """以前の不具合の再現: 週次スクリーナーが約 1 年分の SPY を保存 → 過去データ取得が差分だけになり 15 年分を取らない。"""
    import sys
    import types
    c = cfg(data={"dir": str(tmp_path), "history_start": "2010-01-01"})
    short_idx = pd.bdate_range("2025-06-01", periods=250)
    data.write_prices(c, "SPY", pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                                             index=short_idx))
    calls = []

    def download(tickers, start=None, **kw):
        calls.append((sorted(tickers), start))
        idx = pd.bdate_range(start, periods=300 if start == "2010-01-01" else 5)
        cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Volume"]])
        return pd.DataFrame(2.0, index=idx, columns=cols)
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    data.update_prices(c, ["SPY"])
    assert calls[0] == (["SPY"], "2010-01-01")                      # 全期間を取り直す
    assert data.read_prices(c, "SPY").index[0] == pd.Timestamp("2010-01-01")
    calls.clear()
    data.update_prices(c, ["SPY"])
    assert calls and calls[0][1] != "2010-01-01"                     # 2 回目からは差分だけ


def test_existing_full_files_are_not_redownloaded(tmp_path, monkeypatch):
    import sys
    import types
    c = cfg(data={"dir": str(tmp_path), "history_start": "2010-01-01"})
    data.write_prices(c, "AAA", pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                                             index=pd.bdate_range("2010-01-04", periods=300)))
    calls = []

    def download(tickers, start=None, **kw):
        calls.append(start)
        cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Volume"]])
        return pd.DataFrame(2.0, index=pd.bdate_range(start, periods=5), columns=cols)
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    data.update_prices(c, ["AAA"])
    assert calls and calls[0] != "2010-01-01"           # 以前の版で取得済みの全期間データは差分更新だけ


def test_ticker_NA_is_not_treated_as_missing(tmp_path):
    nasdaq = "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n" \
             "NA|Nano Labs Ltd - Class A Ordinary Shares|Q|N|N|100|N|N\n" \
             "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\nFile Creation Time: x|||||||\n"
    other = "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n" \
            "NULL|Null Corp Common Stock|N|NULL|N|100|N|NULL\nFile Creation Time: x|||||||\n"
    c = cfg(data={"dir": str(tmp_path)})
    df = data.parse_symbol_directory(nasdaq, other, c)
    assert list(df["symbol"]) == ["AAPL", "NA", "NULL"]
    df.to_csv(c.path("universe.csv"), index=False)
    back = data.load_universe(c)
    assert list(back["symbol"]) == ["AAPL", "NA", "NULL"] and all(isinstance(s, str) for s in back["symbol"])


def test_fetch_skips_bad_symbols_and_saves_benchmark_first(tmp_path, monkeypatch):
    import sys
    import types
    c = cfg(data={"dir": str(tmp_path), "history_start": "2010-01-01"})
    order = []

    def download(tickers, start=None, **kw):
        order.extend(tickers)
        cols = pd.MultiIndex.from_product([tickers, ["Open", "High", "Low", "Close", "Volume"]])
        return pd.DataFrame(2.0, index=pd.bdate_range(start, periods=300), columns=cols)
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    data.update_prices(c, ["AAA", float("nan"), "", "SPY"])
    assert order[0] == "SPY" and "AAA" in order and len(order) == 2
    assert data.read_prices(c, "SPY") is not None
