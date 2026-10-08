"""銘柄の自動選定 (スクリーニング)。

スキャルピングに向くのは「よく動いて (値幅)、よく売買されていて (売買代金)、
スプレッドが狭い」銘柄。候補リストの最新スナップショットからこれを点数化して上位を選ぶ。

  点数 = 当日値幅% × log10(売買代金) × 出来高比率 (上限 3)

起動時に 1 回選ぶ (毎日起動し直せば、その日の銘柄になる)。
"""
from __future__ import annotations

import logging
import math

from .config import Config

log = logging.getLogger(__name__)

# 候補リスト (流動性の高い米国株・ETF)。config の auto_symbols.universe で差し替え可
UNIVERSE = [
        "US.NVDA", "US.TSLA", "US.AAPL", "US.AMD", "US.META", "US.AMZN", "US.MSFT", "US.GOOGL", "US.NFLX",
        "US.AVGO", "US.MU", "US.INTC", "US.SMCI", "US.ARM", "US.TSM", "US.QCOM", "US.MRVL", "US.ORCL",
        "US.PLTR", "US.COIN", "US.MSTR", "US.HOOD", "US.SOFI", "US.MARA", "US.RIOT", "US.UBER", "US.SHOP",
        "US.BABA", "US.PDD", "US.NIO", "US.RIVN", "US.LCID", "US.F", "US.BAC", "US.JPM", "US.XOM",
        "US.DIS", "US.BA", "US.NKE", "US.SNOW", "US.CRWD", "US.PANW", "US.NET", "US.RBLX", "US.DKNG",
        "US.AFRM", "US.UPST", "US.IONQ", "US.RKLB", "US.SPY", "US.QQQ", "US.IWM", "US.TQQQ", "US.SQQQ",
        "US.SOXL", "US.SOXS", "US.TSLL", "US.NVDL",
]


def _f(row: dict, key: str) -> float | None:
    v = row.get(key)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def rank(rows: list[dict], cfg: Config) -> list[dict]:
    """スナップショット (dict のリスト) を点数順に並べる。条件外の銘柄は reason 付きで除外扱い。"""
    a = cfg.auto_symbols
    out = []
    for r in rows:
        code = r.get("code")
        last = _f(r, "last_price")
        hi, lo = _f(r, "high_price"), _f(r, "low_price")
        prev = _f(r, "prev_close_price") or last
        turnover = _f(r, "turnover") or 0.0
        bid, ask = _f(r, "bid_price"), _f(r, "ask_price")
        vr = _f(r, "volume_ratio")
        amp = _f(r, "amplitude")
        if amp is None and hi and lo and prev:
            amp = (hi - lo) / prev * 100
        spread = (ask - bid) / last * 100 if (bid and ask and last and ask > bid) else None
        item = {"code": code, "name": str(r.get("name", "")), "last": last, "amplitude": amp,
                "turnover": turnover, "volume_ratio": vr, "spread_pct": spread, "score": 0.0, "excluded": None}
        if not last or last <= 0:
            item["excluded"] = "価格なし"
        elif str(r.get("suspension", "")).lower() in ("true", "1"):
            item["excluded"] = "売買停止"
        elif a.min_price and last < a.min_price:
            item["excluded"] = f"株価 {a.min_price} 未満"
        elif a.max_price and last > a.max_price:
            item["excluded"] = f"株価 {a.max_price} 超"
        elif turnover < a.min_turnover:
            item["excluded"] = "売買代金が少ない"
        elif amp is None or amp < a.min_amplitude_pct:
            item["excluded"] = "値幅が小さい"
        elif spread is not None and a.max_spread_pct and spread > a.max_spread_pct:
            item["excluded"] = "スプレッドが広い"
        else:
            ratio = min(max(vr or 1.0, 0.2), 3.0)
            item["score"] = amp * math.log10(max(turnover, 10.0)) * ratio
        out.append(item)
    out.sort(key=lambda x: (x["excluded"] is not None, -x["score"]))
    return out


def universe_for(cfg: Config, quote_ctx, mm) -> list[str]:
    a = cfg.auto_symbols
    if a.universe:
        return [c.upper() for c in a.universe]
    return list(UNIVERSE)


def screen(cfg: Config, quote_ctx, mm) -> list[dict]:
    """候補リストのスナップショットを取って点数順に返す。"""
    codes = universe_for(cfg, quote_ctx, mm)
    if not codes:
        raise RuntimeError("候補リストが空です (auto_symbols.universe を設定してください)")
    rows: list[dict] = []
    for i in range(0, len(codes), 200):   # スナップショットは 1 回 400 銘柄まで
        ret, df = quote_ctx.get_market_snapshot(codes[i:i + 200])
        if ret != mm.RET_OK:
            raise RuntimeError(f"スナップショットを取得できません (相場権限を確認): {df}")
        rows.extend(r.to_dict() for _, r in df.iterrows())
    return rank(rows, cfg)


def select(cfg: Config, quote_ctx, mm) -> list[str]:
    """上位 count 銘柄を選んでログに理由を出す。"""
    ranked = screen(cfg, quote_ctx, mm)
    chosen = [r for r in ranked if r["excluded"] is None][: cfg.auto_symbols.count]
    if not chosen:
        raise RuntimeError("条件に合う銘柄がありません (auto_symbols の条件を緩めてください)")
    for r in chosen:
        log.info("自動選定: %s %s 値幅=%.2f%% 売買代金=%.3g 出来高比=%s スプレッド=%s 点数=%.1f", r["code"], r["name"],
                 r["amplitude"], r["turnover"], r["volume_ratio"],
                 "—" if r["spread_pct"] is None else f"{r['spread_pct']:.3f}%", r["score"])
    return [r["code"] for r in chosen]
