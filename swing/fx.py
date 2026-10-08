"""為替 (円/ドル) の今の値。

使う順番:
  1. 口座から計算した値 (実口座の 円建て総資産 ÷ ドル建て総資産 = moomoo の換算レート)。範囲外なら使わない
     (模擬口座は円とドルで同じ数字を返すので 1 になる → 使わない)
  2. Yahoo Finance の USD/JPY (ティッカー JPY=X)。取得した値は data.dir/fx.json に保存し、
     account.fx_max_age_hours より古くなったら使わない
  3. config の account.fx_rate_jpy_per_usd

画面の 1.5 秒ごとの更新ではネットに取りに行かない (保存した値を読むだけ)。取りに行くのは refresh() を呼んだとき
(接続チェック・口座の読み込み・発注の処理の前と、画面の起動時)。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

log = logging.getLogger(__name__)

YAHOO_TICKER = "JPY=X"           # 1 ドル = 何円


def sane(cfg, fx: float | None) -> float | None:
    """config の範囲内ならその値、外なら None。"""
    lo, hi = cfg.account["fx_sane_range"]
    try:
        fx = float(fx)
    except (TypeError, ValueError):
        return None
    return fx if lo <= fx <= hi else None


def _path(cfg):
    return cfg.path("fx.json")


def fetch_yahoo() -> float:
    import yfinance as yf
    df = yf.Ticker(YAHOO_TICKER).history(period="5d", interval="1d")
    if df is None or df.empty:
        raise RuntimeError("USD/JPY を取得できません")
    return float(df["Close"].dropna().iloc[-1])


def refresh(cfg, fetch=None) -> dict | None:
    """ネットから今の為替を取り、保存する。失敗しても止めない (前の値・設定値を使う)。"""
    try:
        rate = (fetch or fetch_yahoo)()
    except Exception as e:  # noqa: BLE001
        log.warning("為替 (USD/JPY) を取得できませんでした (前の値か設定値を使います): %s", e)
        return None
    if not sane(cfg, rate):
        log.warning("取得した為替 %.4f 円/ドル は範囲の外なので使いません", rate)
        return None
    rec = {"rate": rate, "source": "Yahoo Finance (USD/JPY)", "time": time.time(),
           "time_text": datetime.now().strftime("%Y-%m-%d %H:%M")}
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    log.info("為替: 1 ドル = %.2f 円 (Yahoo Finance)", rate)
    return rec


def cached(cfg) -> dict | None:
    try:
        rec = json.loads(_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - rec.get("time", 0) > cfg.account["fx_max_age_hours"] * 3600 or not sane(cfg, rec.get("rate")):
        return None
    return rec


def current(cfg, account_fx: float | None = None) -> tuple[float, str]:
    """(1 ドル = 何円, どこの値か)。"""
    if sane(cfg, account_fx):
        return float(account_fx), "口座 (moomoo の換算レート)"
    rec = cached(cfg)
    if rec:
        return float(rec["rate"]), f"{rec['source']}・{rec.get('time_text', '')} 取得"
    return float(cfg.account["fx_rate_jpy_per_usd"]), "設定値 (今の為替を取得できていないため)"
