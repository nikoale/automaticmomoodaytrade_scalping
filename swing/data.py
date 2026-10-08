"""データの取得とキャッシュ。

  銘柄リスト   : Nasdaq Trader の銘柄ディレクトリ (nasdaqlisted.txt / otherlisted.txt。無料・キー不要)
  日足 (10 年+): Yahoo Finance (yfinance。分割・配当調整済み) または Stooq (CSV)
  時価総額など : Yahoo Finance (yfinance の fast_info)
  決算日       : Yahoo Finance (yfinance の get_earnings_dates) または moomoo OpenD (get_earnings_calendar ※要確認)

すべて data/ 以下に CSV でキャッシュし、2 回目以降は差分だけ取得する。
※ 銘柄リストは「今上場している銘柄」なので、バックテストには生存者バイアスがある (README 参照)。
"""
from __future__ import annotations

import io
import json
import logging
import ssl
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"
UA = "Mozilla/5.0 (swing-bot research)"

PRICE_COLS = ["open", "high", "low", "close", "volume"]
# ティッカーに "NA" (実在) などがあるので、pandas に「欠損値」と解釈させない
TEXT_CSV = {"dtype": str, "keep_default_na": False, "na_values": []}


def _ssl_context() -> ssl.SSLContext:
    """HTTPS の証明書。Mac の python.org 版 Python は OS の証明書を使わないため
    (CERTIFICATE_VERIFY_FAILED になる)、certifi の証明書一覧があればそれを使う。検証は省略しない。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
        return r.read()


# ---------------------------------------------------------------- 銘柄リスト
def parse_symbol_directory(nasdaq_txt: str, other_txt: str, cfg) -> pd.DataFrame:
    """Nasdaq Trader のファイルから米国上場の普通株だけを残す。"""
    u = cfg.universe
    rows = []
    nq = pd.read_csv(io.StringIO(nasdaq_txt), sep="|", **TEXT_CSV)
    nq = nq[~nq["Symbol"].str.startswith("File Creation Time")]
    for _, r in nq.iterrows():
        rows.append({"symbol": r["Symbol"], "name": r["Security Name"], "exchange": "NASDAQ",
                     "etf": r.get("ETF", "N"), "test": r.get("Test Issue", "N"),
                     "status": r.get("Financial Status", "")})
    ot = pd.read_csv(io.StringIO(other_txt), sep="|", **TEXT_CSV)
    ot = ot[~ot["ACT Symbol"].str.startswith("File Creation Time")]
    ex_map = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX"}
    for _, r in ot.iterrows():
        rows.append({"symbol": r["ACT Symbol"], "name": r["Security Name"],
                     "exchange": ex_map.get(r["Exchange"], r["Exchange"]), "etf": r.get("ETF", "N"),
                     "test": r.get("Test Issue", "N"), "status": ""})
    df = pd.DataFrame(rows)
    n0 = len(df)
    keep = (df["etf"] == "N") & (df["test"] == "N") & (df["status"].isin(["", "N"]))
    keep &= ~df["symbol"].str.contains(r"[\$\^]", regex=True)          # 優先株などの記号
    keep &= df["name"].str.contains("|".join(u["require_name_patterns"]), case=False, regex=True)
    for pat in u["exclude_name_patterns"]:
        keep &= ~df["name"].str.contains(pat, case=False, regex=False)
    if u["exclude_adr"]:
        keep &= ~df["name"].str.contains("American Depositary|ADR|ADS", case=False, regex=True)
    out = df[keep].drop_duplicates("symbol").sort_values("symbol").reset_index(drop=True)
    log.info("銘柄リスト: %d 件中 %d 件を普通株として採用", n0, len(out))
    return out[["symbol", "name", "exchange"]]


def name_excluded(name: str, cfg) -> bool:
    """SPAC・ワラント・優先株・ADR などを名前で除外する (Nasdaq Trader / moomoo の銘柄名どちらにも使う)。"""
    u = cfg.universe
    low = str(name).lower()
    if any(p.lower() in low for p in u["exclude_name_patterns"]):
        return True
    if u["exclude_adr"] and any(k in low for k in ("american depositary", " adr", " ads")):
        return True
    return False


def update_universe(cfg) -> pd.DataFrame:
    nasdaq = _get(NASDAQ_LISTED).decode("utf-8", "replace")
    other = _get(OTHER_LISTED).decode("utf-8", "replace")
    df = parse_symbol_directory(nasdaq, other, cfg)
    path = cfg.path("universe.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df


def load_universe(cfg) -> pd.DataFrame:
    path = cfg.path("universe.csv")
    if not path.exists():
        raise FileNotFoundError("過去データがまだありません。先に画面の「データ」タブで「過去データを取得・更新する」を押してください"
                                "（コマンドの場合は python -m swing fetch all）")
    df = pd.read_csv(path, **TEXT_CSV)
    return df[df["symbol"].str.strip() != ""]


def yahoo_symbol(sym: str) -> str:
    return sym.replace(".", "-")     # BRK.B → BRK-B


# ---------------------------------------------------------------- 日足
# 日足の置き場は 2 つに分ける:
#   history: バックテスト用 (Yahoo / Stooq の 10 年以上)
#   live   : 運用用 (moomoo OpenD から週次・日次で取る約 1 年分)
# 混ぜると、約 1 年分しかない moomoo の日足を「取得済み」と誤認して 10 年分を取らなくなる
PRICE_DIRS = {"history": "prices", "live": "prices_live"}
FULL_MARKER = "_full_history.json"      # 開始日から全期間を取り終えた銘柄の記録


def price_path(cfg, sym: str, kind: str = "history") -> Path:
    return cfg.path(PRICE_DIRS[kind], f"{sym}.csv")


def _load_full(cfg) -> dict:
    p = cfg.path(PRICE_DIRS["history"], FULL_MARKER)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_full(cfg, full: dict) -> None:
    p = cfg.path(PRICE_DIRS["history"], FULL_MARKER)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(full, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")


def read_prices(cfg, sym: str, kind: str = "history") -> pd.DataFrame | None:
    p = price_path(cfg, sym, kind)
    if not p.exists():
        return None
    df = pd.read_csv(p, parse_dates=["date"], index_col="date")
    return df[PRICE_COLS]


def write_prices(cfg, sym: str, df: pd.DataFrame, kind: str = "history") -> None:
    p = price_path(cfg, sym, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    df = df[PRICE_COLS].dropna(subset=["close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "date"
    df.to_csv(p, float_format="%.6g")


def _merge(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    if old is None or old.empty:
        return new
    return pd.concat([old[old.index < new.index.min()], new]) if not new.empty else old


def fetch_yahoo(symbols: list[str], start: str, chunk: int = 80, pause: float = 1.0) -> dict[str, pd.DataFrame]:
    """yfinance でまとめて取得 (分割・配当調整済みの OHLC)。"""
    import yfinance as yf
    out: dict[str, pd.DataFrame] = {}
    bad = [s for s in symbols if not isinstance(s, str) or not s.strip()]
    if bad:
        log.warning("おかしな銘柄コードを %d 件飛ばしました: %s", len(bad), bad[:5])
        symbols = [s for s in symbols if isinstance(s, str) and s.strip()]
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        ymap = {yahoo_symbol(s): s for s in part}
        df = yf.download(list(ymap), start=start, auto_adjust=True, group_by="ticker", threads=True,
                         progress=False)
        for ysym, sym in ymap.items():
            try:
                sub = df[ysym] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            sub = sub.rename(columns=str.lower)[PRICE_COLS].dropna(subset=["close"])
            if not sub.empty:
                sub.index = pd.to_datetime(sub.index).tz_localize(None)
                out[sym] = sub
        log.info("Yahoo: %d/%d 銘柄", min(i + chunk, len(symbols)), len(symbols))
        time.sleep(pause)
    return out


def fetch_stooq(sym: str) -> pd.DataFrame:
    """Stooq の日足 CSV (1 銘柄ずつ)。調整方法は Stooq 側の仕様に依存 (要確認)。"""
    raw = _get(STOOQ_URL.format(sym=yahoo_symbol(sym).lower())).decode()
    if not raw.startswith("Date"):
        return pd.DataFrame(columns=PRICE_COLS)
    df = pd.read_csv(io.StringIO(raw), parse_dates=["Date"], index_col="Date").rename(columns=str.lower)
    return df[PRICE_COLS]


def update_prices(cfg, symbols: list[str], start: str | None = None) -> int:
    """キャッシュを更新する。

    開始日から全期間を取り終えた銘柄 (_full_history.json に記録) は、最終日の 7 日前から取り直して継ぎ足す。
    記録がない銘柄は (途中までしかない可能性があるので) 開始日から全部取り直す。
    """
    start = start or cfg.data.history_start
    # 市場フィルター用の SPY を先頭に (他の銘柄で問題が起きても必ず取れるように)
    b = cfg.data.benchmark
    symbols = ([b] if b in symbols else []) + [s for s in symbols if s != b]
    full = _load_full(cfg)
    todo_full, todo_inc = [], {}
    start_ts = pd.Timestamp(start)
    for s in symbols:
        old = read_prices(cfg, s)
        if old is not None and not old.empty and full.get(s) != start and \
                old.index.min() <= start_ts + timedelta(days=14):
            full[s] = start          # 記録はないが開始日から揃っている (以前の版で取得済み) → 取り直さない
        if old is None or old.empty or full.get(s) != start:
            todo_full.append(s)
        else:
            todo_inc[s] = (old.index.max() - timedelta(days=7)).strftime("%Y-%m-%d")
    n = 0
    if cfg.data.price_source == "yahoo":
        # 80 銘柄ずつ取って、その都度保存する (途中で止まっても、取れた分は残り、次回は続きから)
        step = 80
        for i in range(0, len(todo_full), step):
            for s, df in fetch_yahoo(todo_full[i:i + step], start).items():
                write_prices(cfg, s, df)             # 全期間で置き換え (途中までのデータは捨てる)
                full[s] = start
                n += 1
            _save_full(cfg, full)
            log.info("日足 (全期間): %d/%d 銘柄", min(i + step, len(todo_full)), len(todo_full))
        inc = list(todo_inc)
        for i in range(0, len(inc), step):
            part = inc[i:i + step]
            for s, df in fetch_yahoo(part, min(todo_inc[x] for x in part)).items():
                write_prices(cfg, s, _merge(read_prices(cfg, s), df))
                n += 1
            log.info("日足 (差分): %d/%d 銘柄", min(i + step, len(inc)), len(inc))
    else:
        for s in todo_full + list(todo_inc):
            try:
                df = fetch_stooq(s)
            except Exception as e:  # noqa: BLE001 - 1 銘柄の失敗で全体を止めない
                log.warning("Stooq %s 取得失敗: %s", s, e)
                continue
            if not df.empty:
                write_prices(cfg, s, df[df.index >= start])        # Stooq は毎回全期間が返る
                full[s] = start
                n += 1
            time.sleep(0.3)
        _save_full(cfg, full)
    log.info("日足を更新: %d 銘柄", n)
    return n


# ---------------------------------------------------------------- 銘柄情報 (時価総額・発行済株数)
def update_meta(cfg, symbols: list[str]) -> pd.DataFrame:
    import yfinance as yf
    rows = []
    for i, s in enumerate(symbols):
        try:
            fi = yf.Ticker(yahoo_symbol(s)).fast_info
            rows.append({"symbol": s, "market_cap": fi.get("marketCap"), "shares": fi.get("shares"),
                         "asof": date.today().isoformat()})
        except Exception as e:  # noqa: BLE001
            log.warning("銘柄情報 %s 取得失敗: %s", s, e)
        if i % 100 == 99:
            log.info("銘柄情報: %d/%d", i + 1, len(symbols))
    df = pd.DataFrame(rows)
    path = cfg.path("meta.csv")
    old = pd.read_csv(path, keep_default_na=False, na_values=[""], dtype={"symbol": str}) \
        if path.exists() else pd.DataFrame(columns=df.columns)
    df = pd.concat([old[~old["symbol"].isin(df["symbol"])], df]).sort_values("symbol")
    df.to_csv(path, index=False)
    return df


def load_meta(cfg) -> pd.DataFrame:
    path = cfg.path("meta.csv")
    if not path.exists():
        return pd.DataFrame(columns=["symbol", "market_cap", "shares", "asof"]).set_index("symbol")
    return pd.read_csv(path, keep_default_na=False, na_values=[""], dtype={"symbol": str}).set_index("symbol")


# ---------------------------------------------------------------- 決算日
def earnings_path(cfg, sym: str) -> Path:
    return cfg.path("earnings", f"{sym}.csv")


def read_earnings(cfg, sym: str) -> list[date] | None:
    """キャッシュ済みの決算日 (過去 + 予定)。未取得なら None。"""
    p = earnings_path(cfg, sym)
    if not p.exists():
        return None
    df = pd.read_csv(p)
    return sorted({datetime.strptime(x, "%Y-%m-%d").date() for x in df["date"].dropna()})


def write_earnings(cfg, sym: str, dates: list[date]) -> None:
    p = earnings_path(cfg, sym)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = set(read_earnings(cfg, sym) or [])
    pd.DataFrame({"date": [d.isoformat() for d in sorted(old | set(dates))]}).to_csv(p, index=False)


def fetch_earnings_yahoo(sym: str, limit: int = 60) -> list[date]:
    """yfinance の決算日 (過去約 15 年 + 次回予定)。"""
    import yfinance as yf
    df = yf.Ticker(yahoo_symbol(sym)).get_earnings_dates(limit=limit)
    if df is None or df.empty:
        return []
    return sorted({pd.Timestamp(x).tz_localize(None).date() if pd.Timestamp(x).tzinfo else pd.Timestamp(x).date()
                   for x in df.index})


def fetch_earnings_moomoo(cfg, begin: date, end: date) -> dict[str, list[date]]:
    """moomoo OpenD の決算カレンダー (get_earnings_calendar)。1 回の期間は 7 日まで。

    要確認: moomoo証券(日本) のアカウント・データプランで利用できるか、取得件数の上限、
    filter_list を省略したとき全銘柄が返るか (SDK ソース上は sort_type 既定 Hot)。
    """
    import moomoo as mm
    ctx = mm.OpenQuoteContext(host=cfg.moomoo.host, port=cfg.moomoo.port)
    out: dict[str, list[date]] = {}
    try:
        d = begin
        while d <= end:
            e = min(d + timedelta(days=6), end)
            ret, df = ctx.get_earnings_calendar(mm.Market.US, begin_date=d.isoformat(), end_date=e.isoformat())
            if ret != mm.RET_OK:
                raise RuntimeError(f"get_earnings_calendar 失敗: {df}")
            for _, r in df.iterrows():
                sym = str(r["security"]).split(".", 1)[-1]
                out.setdefault(sym, []).append(pd.Timestamp(r["earnings_date"]).date())
            d = e + timedelta(days=1)
    finally:
        ctx.close()
    return out


def update_earnings(cfg, symbols: list[str]) -> int:
    n = 0
    if cfg.data.earnings_source == "moomoo":
        got = fetch_earnings_moomoo(cfg, date.today(), date.today() + timedelta(days=40))
        for s in symbols:
            write_earnings(cfg, s, got.get(s, []))
            n += s in got
    else:
        for i, s in enumerate(symbols):
            try:
                write_earnings(cfg, s, fetch_earnings_yahoo(s))
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("決算日 %s 取得失敗: %s", s, e)
            if i % 50 == 49:
                log.info("決算日: %d/%d", i + 1, len(symbols))
    log.info("決算日を更新: %d 銘柄", n)
    return n


# ---------------------------------------------------------------- パネル (日付 × 銘柄)
def load_panel(cfg, symbols: list[str], start: str | None = None, prune: bool = True) -> dict:
    """キャッシュから日足パネルを作る。営業日はベンチマーク (SPY) の日付に揃える。

    prune=True: 6 ヶ月上昇率の「市場全体での順位」は全銘柄で計算してから、
    一度も株価・出来高の条件を満たさない銘柄をメモリ節約のため落とす。
    """
    bench = read_prices(cfg, cfg.data.benchmark)
    if bench is None:
        raise FileNotFoundError(f"{cfg.data.benchmark} の日足がありません。画面の「データ」タブで「過去データを取得・更新する」を押してください"
                                "（コマンドの場合は python -m swing fetch all）")
    idx = bench.index[bench.index >= pd.Timestamp(start)] if start else bench.index
    sc = cfg.screener
    # 1 回目: 終値と出来高だけ読む (全銘柄での順位付けと刈り込み用。メモリ節約のため float32)
    closes, vols, missing = {}, {}, 0
    for s in symbols:
        df = read_prices(cfg, s)
        if df is None or len(df) < sc["sma_slow"]:
            missing += df is None
            continue
        df = df.reindex(idx)
        closes[s] = df["close"].astype("float32")
        vols[s] = df["volume"].astype("float32")
    close = pd.DataFrame(closes, index=idx)
    volume = pd.DataFrame(vols, index=idx)
    mom = close / close.shift(sc["momentum_days"]) - 1.0
    mom_pct = mom.rank(axis=1, pct=True)          # 市場全体 (全銘柄) での順位 0〜1
    keep = list(close.columns)
    if prune:
        av = volume.rolling(sc["avg_volume_days"], min_periods=sc["avg_volume_days"]).mean()
        ok = (close >= sc["price_min"]) & (close <= sc["price_max"]) & (av >= sc["avg_volume_min"])
        keep = list(ok.columns[ok.any(axis=0)])
    # 2 回目: 残す銘柄だけ OHLCV を読む
    cols: dict[str, dict] = {k: {} for k in PRICE_COLS}
    for s in keep:
        df = read_prices(cfg, s).reindex(idx)
        for k in PRICE_COLS:
            cols[k][s] = df[k].astype("float32")
    panel = {k: pd.DataFrame(cols[k], index=idx, columns=keep) for k in PRICE_COLS}
    panel["mom_pct"] = mom_pct[keep]
    b = bench.reindex(idx)
    panel["bench_close"] = b["close"]
    log.info("パネル: %d 営業日 × %d 銘柄 (全 %d 銘柄中。日足なし %d)", len(idx), len(keep), len(close.columns), missing)
    return panel


def synthetic_panel(n_days: int = 900, n_syms: int = 60, seed: int = 0, start: str = "2015-01-02") -> dict:
    """テスト・動作確認用の擬似データ (トレンド銘柄 + ランダム銘柄 + 指数)。実相場ではない。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n_days)
    syms = [f"S{i:03d}" for i in range(n_syms)]
    drift = rng.normal(0.0004, 0.0008, n_syms)
    rets = rng.normal(drift, 0.018, (n_days, n_syms))
    close = 30 * np.exp(np.cumsum(rets, axis=0))
    gap = rng.normal(0, 0.004, (n_days, n_syms))
    open_ = close * np.exp(gap - rets / 2)
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, (n_days, n_syms))))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, (n_days, n_syms))))
    vol = rng.lognormal(13.8, 0.4, (n_days, n_syms)) * (1 + 3 * np.maximum(rets, 0) / 0.018)
    mk = lambda a: pd.DataFrame(a.astype("float32"), index=idx, columns=syms)  # noqa: E731
    panel = {"open": mk(open_), "high": mk(hi), "low": mk(lo), "close": mk(close), "volume": mk(vol)}
    c = panel["close"]
    panel["mom_pct"] = (c / c.shift(126) - 1).rank(axis=1, pct=True)
    bench = 300 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n_days)))
    panel["bench_close"] = pd.Series(bench, index=idx)
    return panel
