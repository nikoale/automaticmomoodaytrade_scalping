"""moomoo OpenD からのデータ取得 (週次スクリーナー・日次データ用)。

関数名・引数は公式 SDK moomoo-api 10.11 のソースと同梱サンプル (examples/simple_filter_demo.py) で確認した。
実機 (moomoo証券(日本) の口座) では未検証。要確認事項は docs/要確認事項.md。

  get_stock_filter          : 条件選股。株価・時価総額・N 日平均出来高・N 日騰落率・移動平均の比較をサーバー側で判定
  get_stock_basicinfo       : 銘柄一覧 (普通株 = SecurityType.STOCK)
  get_earnings_calendar     : 決算カレンダー (1 回 7 日まで)
  request_history_kline     : 日足 (過去 K 線の取得枠を消費する)
  get_history_kl_quota      : 過去 K 線の取得枠 (使用済み / 残り)
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import pandas as pd

log = logging.getLogger(__name__)


# get_stock_filter は絞り込む条件に「下限と上限の両方」が必要 (片側だけだとエラー:
# 「フィルターフィールドに範囲値が設定されていません」。2026-10 実機で確認)。片側を決めない条件にはこの値を使う
OPEN_MAX = 1e15
PCT_MIN = -100.0          # 騰落率 (%) の下限 (−100% より下はない)


def to_code(sym: str) -> str:
    return sym if sym.startswith("US.") else f"US.{sym}"


def to_sym(code: str) -> str:
    return code[3:] if code.startswith("US.") else code


class MoomooData:
    def __init__(self, cfg):
        import moomoo as mm
        self.mm = mm
        self.cfg = cfg
        self.m = cfg["moomoo_data"]
        self.ctx = mm.OpenQuoteContext(host=cfg["moomoo"]["host"], port=cfg["moomoo"]["port"])
        self._last_filter = 0.0

    def close(self) -> None:
        self.ctx.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- 取得枠
    def quota(self) -> tuple[int, int]:
        ret, data = self.ctx.get_history_kl_quota(get_detail=False)
        if ret != self.mm.RET_OK:
            raise RuntimeError(f"get_history_kl_quota 失敗: {data}")
        used, remain, _ = data
        return int(used), int(remain)

    # ---------------------------------------------------------------- 条件選股
    def _filter(self, filters: list, begin: int = 0, num: int = 200, step: str = ""):
        """get_stock_filter (呼び出し間隔を空けて頻度制限を避ける)。step はエラー時に出す段階の名前。"""
        wait = self.m["filter_interval_sec"] - (time.monotonic() - self._last_filter)
        if wait > 0:
            time.sleep(wait)
        ret, data = self.ctx.get_stock_filter(market=self.mm.Market.US, filter_list=filters, begin=begin, num=num)
        self._last_filter = time.monotonic()
        if ret != self.mm.RET_OK:
            raise RuntimeError(f"get_stock_filter 失敗{f'（{step}）' if step else ''}: {data}")
        return data            # (last_page, all_count, list[FilterStockData])

    def _simple(self, field, lo=None, hi=None):
        f = self.mm.SimpleFilter()
        f.stock_field, f.is_no_filter = field, False
        f.filter_min = lo if lo is not None else -OPEN_MAX
        f.filter_max = hi if hi is not None else OPEN_MAX
        return f

    def _acc(self, field, days, lo=None, hi=None, no_filter=False, sort=None):
        f = self.mm.AccumulateFilter()
        f.stock_field, f.days = field, days
        if no_filter:
            f.is_no_filter = True          # 絞り込まずに値だけ受け取る
        else:
            f.is_no_filter = False
            f.filter_min = lo if lo is not None else -OPEN_MAX
            f.filter_max = hi if hi is not None else OPEN_MAX
        if sort is not None:
            f.sort = sort
        return f

    def _ma_above(self, field1, para1, field2, para2):
        """field1(para1) > field2(para2) を日足で判定 (例: PRICE > MA(50), MA(50) > MA(200))。"""
        mm = self.mm
        f = mm.CustomIndicatorFilter()
        f.stock_field1, f.stock_field1_para = field1, para1
        f.stock_field2, f.stock_field2_para = field2, para2
        f.relative_position = mm.RelativePosition.MORE
        f.ktype = mm.KLType.K_DAY
        f.is_no_filter = False
        return f

    def momentum_threshold(self) -> tuple[float, int]:
        """6 ヶ月騰落率が「市場全体の上位 N%」になる境目の値 (%) と、対象銘柄数。

        降順に並べて上位 N% の位置の 1 銘柄だけを取る (2 回の呼び出しで済む)。
        """
        sc, mm = self.cfg["screener"], self.mm
        flt = self._acc(mm.StockField.CHANGE_RATE, sc["momentum_days"], PCT_MIN, OPEN_MAX, sort=mm.SortDir.DESCEND)
        step = "6ヶ月騰落率の上位の境目を求める"
        _, total, _ = self._filter([flt], begin=0, num=1, step=step)
        if total <= 0:
            raise RuntimeError("騰落率の対象銘柄が 0 件です")
        pos = max(int(total * sc["momentum_top_pct"] / 100.0) - 1, 0)
        _, _, items = self._filter([flt], begin=pos, num=1, step=step)
        if not items:
            raise RuntimeError("上位 N% の境目の銘柄を取得できません")
        v = _value(items[0], ("change_rate", sc["momentum_days"]))
        log.info("6ヶ月騰落率: 対象 %d 銘柄、上位 %s%% の境目 = %.2f%%", total, sc["momentum_top_pct"], v)
        return float(v), int(total)

    def screen_candidates(self, threshold_pct: float) -> list[dict]:
        """サーバー側で条件を満たす銘柄 (ページ送りで全件)。出来高の伸び用に 20 日・100 日の平均出来高も受け取る。"""
        sc, mm = self.cfg["screener"], self.mm
        sf = mm.StockField
        filters = [
            self._simple(sf.CUR_PRICE, sc["price_min"], sc["price_max"]),
            self._simple(sf.MARKET_VAL, sc["market_cap_min_usd"], OPEN_MAX),
            self._acc(sf.VOLUME, sc["avg_volume_days"], sc["avg_volume_min"], OPEN_MAX),
            self._acc(sf.VOLUME, sc["volume_ratio_long"], no_filter=True),
            self._acc(sf.CHANGE_RATE, sc["momentum_days"], threshold_pct, OPEN_MAX, sort=mm.SortDir.DESCEND),
            self._ma_above(sf.PRICE, [], sf.MA, [sc["sma_fast"]]),
            self._ma_above(sf.MA, [sc["sma_fast"]], sf.MA, [sc["sma_slow"]]),
        ]
        out, begin = [], 0
        while True:
            last, total, items = self._filter(filters, begin=begin, num=self.m["page_size"],
                                              step="株価・時価総額・出来高・騰落率・移動平均で絞り込む")
            for it in items:
                v20 = _value(it, ("volume", sc["avg_volume_days"]))
                v100 = _value(it, ("volume", sc["volume_ratio_long"]))
                out.append({"code": it.stock_code, "symbol": to_sym(it.stock_code), "name": it.stock_name,
                            "price": _value(it, "cur_price"), "market_cap": _value(it, "market_val"),
                            "avg_vol": v20, "vol_ratio": (v20 / v100) if v20 and v100 else None,
                            "momentum_pct": _value(it, ("change_rate", sc["momentum_days"]))})
            begin += len(items)
            if last or not items or begin >= total:
                break
        log.info("サーバー側スクリーニング: %d 銘柄", len(out))
        return out

    # ---------------------------------------------------------------- 銘柄一覧 (普通株)
    def common_stocks(self) -> pd.DataFrame:
        """普通株 (SecurityType.STOCK) の一覧。ETF は別の種類なので含まれない。"""
        mm = self.mm
        ret, df = self.ctx.get_stock_basicinfo(mm.Market.US, mm.SecurityType.STOCK)
        if ret != mm.RET_OK:
            raise RuntimeError(f"get_stock_basicinfo 失敗: {df}")
        if "delisting" in df.columns:
            df = df[~df["delisting"].astype(bool)]
        return df

    # ---------------------------------------------------------------- 決算カレンダー
    def earnings_calendar(self, begin: date, end: date) -> dict[str, list[date]]:
        mm = self.mm
        out: dict[str, list[date]] = {}
        d = begin
        while d <= end:
            e = min(d + timedelta(days=6), end)
            ret, df = self.ctx.get_earnings_calendar(mm.Market.US, begin_date=d.isoformat(), end_date=e.isoformat())
            if ret != mm.RET_OK:
                raise RuntimeError(f"get_earnings_calendar 失敗: {df}")
            for _, r in df.iterrows():
                out.setdefault(to_sym(str(r["security"])), []).append(pd.Timestamp(r["earnings_date"]).date())
            d = e + timedelta(days=1)
        return {k: sorted(set(v)) for k, v in out.items()}

    # ---------------------------------------------------------------- 日足
    def daily_bars(self, sym: str, start: str, end: str | None = None) -> pd.DataFrame:
        """前方調整 (QFQ) の日足。過去 K 線の取得枠を消費する。"""
        mm = self.mm
        end = end or date.today().isoformat()
        rows, page = [], None
        while True:
            ret, df, page = self.ctx.request_history_kline(to_code(sym), start=start, end=end, ktype=mm.KLType.K_DAY,
                                                           autype=mm.AuType.QFQ, max_count=1000, page_req_key=page)
            if ret != mm.RET_OK:
                raise RuntimeError(f"request_history_kline {sym} 失敗: {df}")
            rows.append(df)
            if page is None:
                break
        df = pd.concat(rows) if rows else pd.DataFrame()
        if df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = df.assign(date=pd.to_datetime(df["time_key"]).dt.normalize()).set_index("date")
        return df[["open", "high", "low", "close", "volume"]].astype(float)


def _value(item, key):
    """FilterStockData から値を取る (SDK は単純項目を属性、累積項目を (名前, 日数) のキーで持つ)。"""
    d = getattr(item, "__dict__", {})
    if key in d:
        return d[key]
    if isinstance(key, str):
        return getattr(item, key, None)
    return None
