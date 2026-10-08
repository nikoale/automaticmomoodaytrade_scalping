"""資金管理: ポジションサイズ・手数料・T+1 受渡・週次の損失上限。

バックテストとライブ (フェーズ 3) で同じ関数を使う。
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date


def commission(value: float, cfg) -> float:
    """片道の手数料: 約定代金 × 0.132%、上限 22 ドル。"""
    f = cfg.fees
    return min(abs(value) * f["commission_pct"] / 100.0, f["commission_max_usd"])


def fx_cost(value: float, cfg) -> float:
    return abs(value) * cfg.account["fx_cost_pct"] / 100.0


def buy_total(shares: float, price: float, cfg) -> float:
    """shares 株を price (スリッページ後) で買うときに必要な現金。"""
    v = shares * price
    return v + commission(v, cfg) + fx_cost(v, cfg)


def sell_net(shares: float, price: float, cfg) -> float:
    v = shares * price
    return v - commission(v, cfg) - fx_cost(v, cfg)


def position_size(equity: float, price: float, atr: float, cfg, cash_available: float | None = None) -> float:
    """株数 = (資金 × 2%) / (ATR × 2)。1 銘柄は資金の 40% まで。現金が足りなければ減らす。整数に切り捨て。"""
    r, st = cfg.risk, cfg.strategy
    if not (price > 0 and atr > 0 and equity > 0):
        return 0
    risk_amount = equity * r["risk_per_trade_pct"] / 100.0
    by_risk = risk_amount / (atr * st["initial_stop_atr"])
    by_cap = equity * r["max_position_pct"] / 100.0 / price
    shares = min(by_risk, by_cap)
    if cash_available is not None:
        unit = buy_total(1.0, price, cfg)
        shares = min(shares, cash_available / unit)
    if r["allow_fractional"]:
        return max(math.floor(shares * 1e4) / 1e4, 0.0)
    n = math.floor(shares + 1e-9)
    return n if n >= 1 else 0


class SettlementLedger:
    """現金口座の受渡管理。売却代金は settlement_days 営業日後から使える。

    key は比較できるもの (バックテスト: 営業日の通し番号 / ライブ: date)。
    """

    def __init__(self, cash: float, settlement_days: int):
        self.settled = cash
        self.pending: list[tuple[object, float]] = []   # (使えるようになる key, 金額)
        self.days = settlement_days

    def settle(self, key) -> None:
        done = [a for k, a in self.pending if k <= key]
        self.pending = [(k, a) for k, a in self.pending if k > key]
        self.settled += sum(done)

    def available(self, key) -> float:
        """key の時点で使える現金 (受渡済み)。"""
        return self.settled + sum(a for k, a in self.pending if k <= key)

    def spend(self, amount: float) -> None:
        if amount > self.settled + 1e-6:
            raise ValueError(f"受渡済み現金 {self.settled:.2f} を超える支払い {amount:.2f}")
        self.settled -= amount

    def add_sale(self, available_key, amount: float) -> None:
        self.pending.append((available_key, amount))

    @property
    def total(self) -> float:
        return self.settled + sum(a for _, a in self.pending)


class WeeklyLossGuard:
    """その週の損失が週初めの資金の 6% に達したら、その週は新規エントリーを止める。"""

    def __init__(self, cfg):
        self.limit = cfg.risk["weekly_loss_limit_pct"] / 100.0
        self.start_equity: dict[tuple, float] = {}
        self.tripped: dict[tuple, bool] = defaultdict(bool)

    @staticmethod
    def week_key(d: date) -> tuple:
        y, w, _ = d.isocalendar()
        return (y, w)

    def start_week(self, d: date, equity: float) -> None:
        self.start_equity.setdefault(self.week_key(d), equity)

    def update(self, d: date, equity: float) -> bool:
        """引けの資金で判定。今週の新規が止まっていれば True。"""
        k = self.week_key(d)
        start = self.start_equity.get(k)
        if start and (equity - start) / start <= -self.limit:
            self.tripped[k] = True
        return self.tripped[k]

    def blocked(self, d: date) -> bool:
        return self.tripped[self.week_key(d)]
