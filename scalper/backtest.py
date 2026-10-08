"""CSV の足データでバックテストし、成績を集計する。"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .broker import SimBroker
from .config import Config
from .data import resample
from .engine import SymbolEngine
from .market import TradingSessions
from .models import Bar, Trade
from .risk import RiskManager
from .strategies import create_strategy


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: list[tuple]   # (time, cumulative pnl)

    def stats(self) -> dict:
        t = self.trades
        n = len(t)
        wins = [x.pnl for x in t if x.pnl > 0]
        losses = [x.pnl for x in t if x.pnl <= 0]
        total = sum(x.pnl for x in t)
        gross_win, gross_loss = sum(wins), -sum(losses)
        peak = dd = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        days = {}
        for x in t:
            days.setdefault(x.exit_time.date(), 0.0)
            days[x.exit_time.date()] += x.pnl
        reasons = {}
        for x in t:
            reasons[x.reason] = reasons.get(x.reason, 0) + 1
        return {
            "trades": n,
            "win_rate": len(wins) / n if n else 0.0,
            "total_pnl": total,
            "avg_pnl": total / n if n else 0.0,
            "avg_win": gross_win / len(wins) if wins else 0.0,
            "avg_loss": -gross_loss / len(losses) if losses else 0.0,
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win else 0.0,
            "max_drawdown": dd,
            "avg_bars_held": sum(x.bars_held for x in t) / n if n else 0.0,
            "days": len(days),
            "winning_days": sum(1 for v in days.values() if v > 0),
            "exit_reasons": reasons,
        }

    def save_trades(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["code", "direction", "qty", "entry_time", "entry_price", "exit_time", "exit_price",
                        "pnl", "reason", "bars_held"])
            for x in self.trades:
                w.writerow([x.code, x.direction, x.qty, x.entry_time, x.entry_price, x.exit_time,
                            x.exit_price, round(x.pnl, 2), x.reason, x.bars_held])


def run_backtest(cfg: Config, data: dict[str, list[Bar]]) -> BacktestResult:
    """data: {銘柄コード: 1 分足リスト}。全銘柄を時刻順に混ぜて 1 本のタイムラインで処理する。"""
    risk = RiskManager(cfg.risk)
    broker = SimBroker(cfg.execution, cfg.market)
    sessions = TradingSessions.from_config(cfg.session)
    engines = {
        code: SymbolEngine(code, cfg, create_strategy(cfg.strategy.name, cfg.strategy.params), risk, broker,
                           sessions)
        for code in data
    }
    timeline = []
    for code, bars in data.items():
        for b in resample(bars, cfg.bar_minutes):
            timeline.append((b.time, code, b))
    timeline.sort(key=lambda x: (x[0], x[1]))

    equity = 0.0
    curve: list[tuple] = []
    seen = 0
    for t, code, bar in timeline:
        eng = engines[code]
        eng.on_bar(bar)
        new = sum(len(e.trades) for e in engines.values())
        if new != seen:
            equity = sum(x.pnl for e in engines.values() for x in e.trades)
            curve.append((t, equity))
            seen = new
    # データ終端で残ったポジションは最終値で決済
    for eng in engines.values():
        if eng.position is not None and timeline:
            eng.force_flatten(timeline[-1][0], "end_of_data")
    trades = sorted((x for e in engines.values() for x in e.trades), key=lambda x: x.exit_time)
    if trades:
        curve.append((trades[-1].exit_time, sum(x.pnl for x in trades)))
    return BacktestResult(trades, curve)


def format_stats(stats: dict, currency: str = "") -> str:
    lines = [
        f"取引回数        : {stats['trades']}",
        f"勝率            : {stats['win_rate'] * 100:.1f}%",
        f"損益合計        : {stats['total_pnl']:,.0f}{currency}",
        f"平均損益        : {stats['avg_pnl']:,.1f}{currency}",
        f"平均利益/平均損失: {stats['avg_win']:,.1f} / {stats['avg_loss']:,.1f}",
        f"プロフィットF   : {stats['profit_factor']:.2f}",
        f"最大ドローダウン: {stats['max_drawdown']:,.0f}{currency}",
        f"平均保有本数    : {stats['avg_bars_held']:.1f}",
        f"勝ち日 / 日数   : {stats['winning_days']} / {stats['days']}",
        f"決済理由        : {stats['exit_reasons']}",
    ]
    return "\n".join(lines)
