"""フェーズ 2: 口座シミュレーション型のバックテスト。

1 営業日の流れ (D = その日):
  寄り付き : 前日の引けで決めた 手仕舞い → 新規買い の順に成行で約定 (スリッページ 0.1% 不利)
             新規買いは「受渡済みの現金」の範囲で。同じ寄り付きで売った代金は T+1 なので使えない
  日中     : 口座側の逆指値 (損切り) に安値が触れたら約定 (寄り付きで下回っていれば寄り付き値)
  引け     : 資金を時価評価 → 損切り価格の更新 → 手仕舞い判定 → 新規エントリー判定 (翌寄り付きで執行)
  週末     : 金曜の引けでスクリーナーを実行し、翌週の監視リストにする

手数料は片道 約定代金 × 0.132% (上限 22 ドル)、両替コストは config。資金は USD で管理し、レポートで円換算する。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from . import calendar_us, risk, strategy
from .screener import screen_at
from .strategy import Position

log = logging.getLogger(__name__)


@dataclass
class Trade:
    symbol: str
    entry_date: date
    exit_date: date
    shares: float
    entry_price: float
    exit_price: float
    cost: float          # 購入総額 (手数料込み)
    proceeds: float      # 売却手取り (手数料込み)
    pnl: float
    reason: str
    held_days: int
    rank: int


@dataclass
class Result:
    equity: pd.Series                       # 引けの資金 (USD)
    cash: pd.Series                         # 現金 (受渡待ち含む)
    n_positions: pd.Series
    trades: list[Trade]
    decisions: list[dict] = field(default_factory=list)
    initial: float = 0.0
    label: str = ""
    skipped: dict = field(default_factory=dict)   # 見送り理由の集計


def _is_week_end(dates: pd.DatetimeIndex, i: int) -> bool:
    if i + 1 >= len(dates):
        return True
    return dates[i].isocalendar()[:2] != dates[i + 1].isocalendar()[:2]


def _v(df: pd.DataFrame, i: int, s: str):
    x = df.iat[i, df.columns.get_loc(s)]
    return None if pd.isna(x) else float(x)


def run(cfg, ind: dict, earnings: dict[str, list[date]] | None = None, shares: dict[str, float] | None = None,
        start: str | None = None, end: str | None = None, index_filter: bool | None = None,
        label: str = "", record_decisions: bool = True) -> Result:
    st, fees = cfg.strategy, cfg.fees
    if index_filter is not None:
        cfg = _with(cfg, "strategy", "index_filter", bool(index_filter))
        st = cfg.strategy
    earnings = earnings or {}
    dates = ind["close"].index
    i0 = int(dates.searchsorted(pd.Timestamp(start or cfg.backtest["start"])))
    end_ = end or cfg.backtest["end"]
    i1 = int(dates.searchsorted(pd.Timestamp(end_), side="right")) - 1 if end_ else len(dates) - 1
    if i0 > i1:
        raise ValueError("バックテスト期間にデータがありません")
    o, h, lo, c, v = (ind[k] for k in ("open", "high", "low", "close", "volume"))
    atr, sma_tr, hprev, avprev = ind["atr"], ind["sma_trail"], ind["high_prev"], ind["avg_vol_prev"]
    slip = fees["slippage_pct"] / 100.0
    capital = cfg.account["capital_jpy"] / cfg.account["fx_rate_jpy_per_usd"]
    ledger = risk.SettlementLedger(capital, cfg.account["settlement_days"])
    guard = risk.WeeklyLossGuard(cfg)
    positions: dict[str, Position] = {}
    last_close: dict[str, float] = {}
    pending_exit: dict[str, str] = {}
    pending_entry: list[dict] = []
    cooldown_until: dict[str, int] = {}
    trades: list[Trade] = []
    decisions: list[dict] = []
    skipped: dict[str, int] = {}
    eq_s, cash_s, npos_s = [], [], []

    def note(i, kind, sym, **kw):
        if record_decisions:
            decisions.append({"date": dates[i].date().isoformat(), "type": kind, "symbol": sym, **kw})

    def close_position(i, sym, raw_price, reason):
        pos = positions.pop(sym)
        px = raw_price * (1 - slip)
        net = risk.sell_net(pos.shares, px, cfg)
        ledger.add_sale(i + cfg.account["settlement_days"], net)
        t = Trade(sym, pos.entry_date, dates[i].date(), pos.shares, pos.entry_price, px, pos.entry_cost, net,
                  net - pos.entry_cost, reason, pos.held_days(i), pos.rank)
        trades.append(t)
        cooldown_until[sym] = i + st["reentry_cooldown_days"]
        note(i, "exit", sym, reason=reason, price=round(px, 4), shares=pos.shares, pnl=round(t.pnl, 2))

    # 開始時点の監視リスト = 開始日より前の直近の週末に作ったもの
    watch: list[dict] = []
    for j in range(i0 - 1, max(i0 - 8, 0), -1):
        if _is_week_end(dates, j):
            watch = screen_at(ind, j, cfg, earnings=earnings, shares=shares).items
            break

    for i in range(i0, i1 + 1):
        d = dates[i].date()
        ledger.settle(i)
        if i == i0 or _is_week_end(dates, i - 1):
            guard.start_week(d, eq_s[-1] if eq_s else capital)

        # ---- 寄り付き: 手仕舞い → 新規
        for sym, reason in list(pending_exit.items()):
            if sym not in positions:
                pending_exit.pop(sym)
                continue
            op = _v(o, i, sym)
            if op is None:
                continue                      # 売買なし (休止など) → 翌日に持ち越し
            close_position(i, sym, op, reason)
            pending_exit.pop(sym)
        for order in pending_entry:
            sym = order["symbol"]
            op = _v(o, i, sym)
            if op is None or sym in positions or len(positions) >= st["max_positions"]:
                skipped["寄り付きで約定できず"] = skipped.get("寄り付きで約定できず", 0) + 1
                continue
            px = op * (1 + slip)
            n = min(order["shares"], risk.position_size(order["equity"], px, order["atr"], cfg, ledger.available(i)))
            if n <= 0:
                skipped["現金不足 (T+1 受渡待ち含む)"] = skipped.get("現金不足 (T+1 受渡待ち含む)", 0) + 1
                note(i, "skip", sym, reason="cash")
                continue
            cost = risk.buy_total(n, px, cfg)
            ledger.spend(cost)
            stop = strategy.initial_stop(px, order["atr"], cfg)
            positions[sym] = Position(sym, n, d, i, px, order["atr"], stop, px, entry_cost=cost, rank=order["rank"])
            note(i, "entry", sym, price=round(px, 4), shares=n, stop=round(stop, 4), rank=order["rank"])
        pending_entry = []

        # ---- 日中: 口座側の逆指値
        for sym in list(positions):
            pos = positions[sym]
            fill = strategy.stop_fill(_v(o, i, sym), _v(lo, i, sym), pos.stop)
            if fill is not None:
                pending_exit.pop(sym, None)
                close_position(i, sym, fill, "trailing_stop" if pos.trailing else "stop")

        # ---- 引け: 時価評価
        mv = 0.0
        for sym, pos in positions.items():
            cl = _v(c, i, sym)
            if cl is not None:
                last_close[sym] = cl
            mv += pos.shares * last_close.get(sym, pos.entry_price)
        equity = ledger.total + mv
        eq_s.append(equity)
        cash_s.append(ledger.total)
        npos_s.append(len(positions))
        blocked_week = guard.update(d, equity)

        # ---- 引け: 損切り更新・手仕舞い判定
        b, bs = ind["bench_close"].iloc[i], ind["bench_sma"].iloc[i]
        index_ok = bool(pd.notna(b) and pd.notna(bs) and b >= bs)
        for sym, pos in positions.items():
            cl = _v(c, i, sym)
            if cl is None:
                continue
            old = pos.stop
            strategy.update_stop(pos, cl, _v(h, i, sym), _v(sma_tr, i, sym), _v(atr, i, sym), cfg)
            if pos.stop > old + 1e-9:
                note(i, "stop_update", sym, old=round(old, 4), new=round(pos.stop, 4))
            nxt, _ok = _next_earn(earnings.get(sym), d)
            edays = calendar_us.trading_days_between(d, nxt) if nxt else None
            reason = strategy.exit_reason(pos, i, cl, edays, index_ok, cfg)
            if reason and sym not in pending_exit:
                pending_exit[sym] = reason
                note(i, "exit_signal", sym, reason=reason)

        # ---- 引け: 新規エントリー判定 (翌寄り付きで執行)
        if i < i1:
            can_enter = (index_ok or not st["index_filter"]) and not blocked_week
            if not can_enter and watch:
                why = "指数フィルター" if not (index_ok or not st["index_filter"]) else "週次の損失上限"
                skipped[f"新規停止 ({why})"] = skipped.get(f"新規停止 ({why})", 0) + 1
            free = st["max_positions"] - (len(positions) - len(pending_exit))
            planned = 0.0
            for item in watch if can_enter else []:
                if free <= 0:
                    break
                sym = item["symbol"]
                if sym in positions or sym not in c.columns or cooldown_until.get(sym, -1) >= i + 1:
                    continue
                if not strategy.entry_signal(_v(c, i, sym), _v(hprev, i, sym), _v(v, i, sym), _v(avprev, i, sym), cfg):
                    continue
                a, cl = _v(atr, i, sym), _v(c, i, sym)
                cash_next = ledger.available(i + 1) - planned
                n = risk.position_size(equity, cl, a, cfg, cash_next)
                if n <= 0:
                    skipped["サイズ 1 株未満 / 現金不足"] = skipped.get("サイズ 1 株未満 / 現金不足", 0) + 1
                    note(i, "skip", sym, reason="size<1 or cash")
                    continue
                planned += risk.buy_total(n, cl * (1 + slip), cfg)
                pending_entry.append({"symbol": sym, "shares": n, "atr": a, "equity": equity, "rank": item["rank"]})
                note(i, "entry_signal", sym, close=round(cl, 4), shares=n, rank=item["rank"])
                free -= 1

        # ---- 週末: 翌週の監視リスト
        if _is_week_end(dates, i):
            res = screen_at(ind, i, cfg, earnings=earnings, shares=shares)
            watch = res.items
            note(i, "watchlist", "", index_ok=res.index_ok, symbols=[x["symbol"] for x in watch])

    # 期間の終わりで残っているポジションは最終日の終値で評価 (売却扱いにしてトレードに含める)
    for sym in list(positions):
        close_position(i1, sym, last_close.get(sym, positions[sym].entry_price) / (1 - slip), "end_of_test")
    idx = dates[i0:i1 + 1]
    eq = pd.Series(eq_s, index=idx)
    if trades and trades[-1].reason == "end_of_test":
        eq.iloc[-1] = ledger.total
    return Result(eq, pd.Series(cash_s, index=idx), pd.Series(npos_s, index=idx), trades, decisions, capital, label,
                  skipped)


def _next_earn(dates, d):
    from .screener import next_earnings
    return next_earnings(dates, d)


def _with(cfg, section: str, key: str, value):
    from .config import Config
    new = Config({k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()})
    new[section][key] = value
    return new


# ---------------------------------------------------------------- 評価指標
def stats(res: Result) -> dict:
    eq = res.equity
    t = res.trades
    if eq.empty:
        return {}
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    final = float(eq.iloc[-1])
    peak = eq.cummax()
    dd = (eq / peak - 1.0)
    wins = [x.pnl for x in t if x.pnl > 0]
    losses = [x.pnl for x in t if x.pnl <= 0]
    yearly = eq.groupby(eq.index.year).last()
    prev = pd.Series([res.initial] + list(yearly.iloc[:-1]), index=yearly.index)
    yearly_pnl = (yearly - prev)
    cash_ratio = (res.cash / res.equity).clip(0, 1)
    return {
        "期間": f"{eq.index[0].date()} 〜 {eq.index[-1].date()}",
        "初期資金_usd": res.initial,
        "最終資金_usd": final,
        "総損益_usd": final - res.initial,
        "総損益_pct": (final / res.initial - 1) * 100,
        "年率リターン_pct": ((final / res.initial) ** (1 / years) - 1) * 100 if final > 0 else -100.0,
        "最大ドローダウン_pct": float(dd.min()) * 100,
        "取引回数": len(t),
        "勝率_pct": len(wins) / len(t) * 100 if t else 0.0,
        "平均利益_usd": float(np.mean(wins)) if wins else 0.0,
        "平均損失_usd": float(np.mean(losses)) if losses else 0.0,
        "損益レシオ": (float(np.mean(wins)) / abs(float(np.mean(losses)))) if wins and losses and np.mean(losses) else None,
        "プロフィットファクター": (sum(wins) / abs(sum(losses))) if losses and sum(losses) else None,
        "平均保有日数": float(np.mean([x.held_days for x in t])) if t else 0.0,
        "現金比率_平均_pct": float(cash_ratio.mean()) * 100,
        "ノーポジション日数_pct": float((res.n_positions == 0).mean()) * 100,
        "手仕舞い理由": pd.Series([x.reason for x in t]).value_counts().to_dict() if t else {},
        "年別損益_usd": {int(k): float(val) for k, val in yearly_pnl.items()},
        "見送り": dict(res.skipped),
    }


def trades_frame(res: Result) -> pd.DataFrame:
    return pd.DataFrame([t.__dict__ for t in res.trades])
