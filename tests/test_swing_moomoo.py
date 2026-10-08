"""moomoo 版週次スクリーナーのテスト (OpenD のフェイクで。戻り値の形は SDK 10.11 のソースに合わせた)。"""
import json
import sys
import types
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from swing import calendar_us
from swing_helpers import cfg as make_cfg

LAST = date(2026, 10, 9)                      # 金曜
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=calendar_us.TOKYO)   # 土曜 9:00 JST
DATES = pd.bdate_range(end=pd.Timestamp(LAST), periods=320)


def _series(kind: str, base=30.0, vol=1e6, recent_vol=None):
    n = len(DATES)
    t = np.arange(n)
    if kind == "up":
        c = base * (1 + 0.004 * t)
    elif kind == "down":
        c = base * (1 + 0.004 * (n - t))
    else:
        c = np.full(n, base)
    v = np.full(n, vol)
    if recent_vol:
        v[-20:] = recent_vol
    df = pd.DataFrame({"time_key": [d.strftime("%Y-%m-%d 00:00:00") for d in DATES], "open": c, "high": c * 1.01,
                       "low": c * 0.99, "close": c, "volume": v})
    return df


class _Item:
    """FilterStockData と同じ持ち方: 単純項目は属性名、累積項目は (名前, 日数) のキー。"""

    def __init__(self, code, name, vals: dict):
        self.stock_code, self.stock_name = code, name
        for k, v in vals.items():
            self.__dict__[k] = v


def build_fake(bench_kind="up"):
    m = types.ModuleType("moomoo")
    m.RET_OK, m.RET_ERROR = 0, -1
    E = types.SimpleNamespace
    m.Market = E(US="US")
    m.SecurityType = E(STOCK="STOCK")
    m.SortDir = E(DESCEND="DESCEND", ASCEND="ASCEND")
    m.RelativePosition = E(MORE="MORE")
    m.KLType = E(K_DAY="K_DAY")
    m.AuType = E(QFQ="QFQ")
    m.StockField = E(CUR_PRICE="CUR_PRICE", MARKET_VAL="MARKET_VAL", VOLUME="VOLUME", CHANGE_RATE="CHANGE_RATE",
                     PRICE="PRICE", MA="MA")

    class _F:
        def __init__(self):
            self.sort = None
    m.SimpleFilter = m.AccumulateFilter = m.CustomIndicatorFilter = _F

    bars = {"US.SPY": _series(bench_kind, 400), "US.GOOD1": _series("up", 25, recent_vol=3e6),
            "US.GOOD2": _series("up", 20, recent_vol=1.5e6), "US.FAKE": _series("down", 20),
            "US.EARN": _series("up", 22, recent_vol=2e6), "US.ADRX": _series("up", 30), "US.SPACX": _series("up", 30),
            "US.ETFY": _series("up", 30)}
    server = [  # サーバー側の条件選股を通った (という想定の) 銘柄
        _Item(c, c, {"cur_price": 50.0, "market_val": 2e9, ("volume", 20): 2e6, ("volume", 100): 1e6,
                     ("change_rate", 126): 60.0})
        for c in ("US.GOOD1", "US.GOOD2", "US.FAKE", "US.EARN", "US.ADRX", "US.SPACX", "US.ETFY")]

    class Ctx:
        last = None

        def __init__(self, **kw):
            self.kline_calls = []
            self.filter_calls = []
            Ctx.last = self

        def get_history_kl_quota(self, get_detail=False):
            return 0, (10, 90, [])

        def get_stock_filter(self, market, filter_list, begin=0, num=200, plate_code=None):
            # 実機と同じ: 絞り込む条件 (is_no_filter=False) に下限・上限の両方がないとエラー
            for f in filter_list:
                if getattr(f, "is_no_filter", None) is False and hasattr(f, "filter_min") and \
                        (getattr(f, "filter_min", None) is None or getattr(f, "filter_max", None) is None):
                    return -1, "フィルターフィールドに範囲値が設定されていません"
            self.filter_calls.append((len(filter_list), begin, num))
            if len(filter_list) == 1:                                  # 騰落率の境目を求める呼び出し
                return 0, (False, 1000, [_Item("US.TH", "TH", {("change_rate", 126): 35.0})])
            page = server[begin:begin + num]
            return 0, (begin + num >= len(server), len(server), page)

        def get_stock_basicinfo(self, market, stock_type):
            return 0, pd.DataFrame({"code": ["US.GOOD1", "US.GOOD2", "US.FAKE", "US.EARN", "US.ADRX", "US.SPACX"],
                                    "name": ["Good One", "Good Two", "Fake", "Earn Co", "Foo Ltd ADR",
                                             "Bar Acquisition Corp"], "delisting": [False] * 6})

        def get_earnings_calendar(self, market, begin_date=None, end_date=None, **kw):
            b, e = date.fromisoformat(begin_date), date.fromisoformat(end_date)
            ed = date(2026, 10, 15)                                    # 4 営業日後
            rows = [{"security": "US.EARN", "earnings_date": ed.isoformat()}] if b <= ed <= e else []
            return 0, pd.DataFrame(rows, columns=["security", "earnings_date"])

        def request_history_kline(self, code, start=None, end=None, ktype=None, autype=None, max_count=1000,
                                  page_req_key=None):
            self.kline_calls.append(code)
            df = bars[code]
            # 2 ページに分けて返す (ページ送りの確認)
            if page_req_key is None:
                return 0, df.iloc[:200], "p2"
            return 0, df.iloc[200:], None

        def close(self):
            pass
    m.OpenQuoteContext = Ctx
    return m


@pytest.fixture
def setup(monkeypatch, tmp_path):
    def go(bench_kind="up"):
        m = build_fake(bench_kind)
        monkeypatch.setitem(sys.modules, "moomoo", m)
        c = make_cfg(data={"dir": str(tmp_path), "screener_source": "moomoo"},
                     moomoo_data={"filter_interval_sec": 0, "page_size": 3})
        return m, c
    return go


def test_moomoo_weekly_screener_end_to_end(setup):
    from swing.screener import run_weekly
    m, c = setup()
    path = run_weekly(c, now_jst=NOW)
    wl = json.loads(open(path, encoding="utf-8").read())
    assert path.name == "watchlist_20261010.json" and wl["data_date"] == "2026-10-09"
    syms = [x["symbol"] for x in wl["items"]]
    # ADR・SPAC は名前で、ETF は普通株一覧にないので除外。FAKE は日足で確かめ直すと下降トレンド。EARN は決算 4 営業日後
    assert syms == ["GOOD1", "GOOD2"]                       # 出来高の伸び (自前計算) の順
    assert wl["items"][1]["next_earnings"] is None and wl["items"][0]["atr_14"] > 0
    assert wl["counts"]["moomoo 条件選股"] == 7 and wl["counts"]["普通株 (名前で除外後)"] == 4
    assert "35.00%" in wl["note"]
    ctx = m.OpenQuoteContext.last
    assert sorted(set(ctx.kline_calls)) == ["US.EARN", "US.FAKE", "US.GOOD1", "US.GOOD2", "US.SPY"]  # 取得枠は候補だけ
    assert [x[1] for x in ctx.filter_calls if x[0] > 1] == [0, 3, 6]                               # ページ送り
    # 日次処理用に日足をキャッシュ
    from swing import data
    assert data.read_prices(c, "GOOD1") is not None and len(data.read_prices(c, "SPY")) == 320


def test_moomoo_screener_market_filter_skips_candidates(setup):
    from swing.screener import run_weekly
    m, c = setup(bench_kind="down")
    wl = json.loads(open(run_weekly(c, now_jst=NOW), encoding="utf-8").read())
    assert wl["items"] == [] and wl["index_ok"] is False and "新規停止" in wl["note"]
    ctx = m.OpenQuoteContext.last
    assert ctx.kline_calls == ["US.SPY", "US.SPY"] and ctx.filter_calls == []   # 候補の日足・条件選股は呼ばない


def test_quota_and_name_filter(setup):
    m, c = setup()
    from swing import data
    from swing.moomoo_data import MoomooData
    with MoomooData(c) as md:
        assert md.quota() == (10, 90)
    assert data.name_excluded("Foo Acquisition Corp - Class A", c)
    assert data.name_excluded("Some Co ADR", c)
    assert not data.name_excluded("Apple Inc.", c)
