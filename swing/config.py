"""config.yaml の読み込み。未知のキーや欠けたキーはエラーにする (設定ミスで黙って動かないように)。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "config.yaml"

REQUIRED = {
    "account": ["capital_jpy", "fx_rate_jpy_per_usd", "fx_sane_range", "fx_max_age_hours", "fx_cost_pct", "settlement_days", "capital_source"],
    "fees": ["commission_pct", "commission_max_usd", "slippage_pct"],
    "data": ["dir", "price_source", "history_start", "benchmark", "earnings_source", "screener_source"],
    "moomoo_data": ["page_size", "filter_interval_sec", "candidates", "bars_calendar_days", "earnings_lookahead_days"],
    "universe": ["require_name_patterns", "exclude_adr", "exclude_name_patterns"],
    "screener": ["price_min", "price_max", "avg_volume_days", "avg_volume_min", "market_cap_min_usd",
                 "earnings_exclude_days", "earnings_unknown_policy", "sma_fast", "sma_slow", "momentum_days",
                 "momentum_top_pct", "volume_ratio_short", "volume_ratio_long", "top_n", "index_sma",
                 "breakout_days", "atr_days"],
    "strategy": ["breakout_days", "breakout_volume_mult", "max_positions", "reentry_cooldown_days", "atr_days",
                 "initial_stop_atr", "trail_trigger_atr", "trail_sma", "trail_atr", "time_exit_days",
                 "time_exit_min_gain_atr", "earnings_exit_days_before", "index_filter"],
    "risk": ["risk_per_trade_pct", "max_position_pct", "weekly_loss_limit_pct", "allow_fractional"],
    "executor": ["state_dir", "order_timing", "stop_time_in_force", "fill_wait_sec", "poll_sec", "on_stop_failure",
                 "max_orders_per_run", "watchlist_max_age_days", "earnings_refresh_days", "test_symbol"],
    "schedule": ["daily_run", "open_run_delay_min", "open_run_max_late_min", "weekly_screen_day", "weekly_screen_time",
                 "retry_count", "retry_min", "tick_sec", "notify"],
    "backtest": ["start", "end", "out_of_sample_years", "stress_periods", "market_cap_proxy", "report_dir"],
}


class Config(dict):
    """dict を属性でも読めるようにしたもの: cfg.screener.price_min"""

    def __getattr__(self, key: str) -> Any:
        try:
            v = self[key]
        except KeyError:
            raise AttributeError(key) from None
        return Config(v) if isinstance(v, dict) and not isinstance(v, Config) else v

    def path(self, *parts: str) -> Path:
        """データ置き場 (data.dir) の下のパス。"""
        return resolve(self["data"]["dir"]).joinpath(*parts)

    def report_dir(self) -> Path:
        return resolve(self["backtest"]["report_dir"])

    def log_dir(self) -> Path:
        return resolve(self["logging"]["dir"] if "logging" in self else "logs")


def resolve(p: str | Path) -> Path:
    """~ (ホーム) を展開し、相対パスはプログラムのフォルダ基準にする。"""
    p = Path(p).expanduser()
    return p if p.is_absolute() else ROOT / p


def load(path: str | Path | None = None, overrides: dict | None = None) -> Config:
    with open(path or DEFAULT_PATH, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    for sec, vals in (overrides or {}).items():
        data.setdefault(sec, {}).update(vals)
    for sec, keys in REQUIRED.items():
        if sec not in data:
            raise ValueError(f"config: [{sec}] がありません")
        missing = [k for k in keys if k not in data[sec]]
        if missing:
            raise ValueError(f"config: [{sec}] に {missing} がありません")
        unknown = [k for k in data[sec] if k not in keys]
        if unknown:
            raise ValueError(f"config: [{sec}] に不明なキー {unknown}")
    if data["screener"]["earnings_unknown_policy"] not in ("exclude", "keep"):
        raise ValueError("screener.earnings_unknown_policy は exclude / keep")
    if data["account"]["capital_source"] not in ("account", "config"):
        raise ValueError("account.capital_source は account / config")
    if data["data"]["screener_source"] not in ("moomoo", "free"):
        raise ValueError("data.screener_source は moomoo / free")
    ex = data["executor"]
    if ex["order_timing"] not in ("reserve", "at_open"):
        raise ValueError("executor.order_timing は reserve / at_open")
    if ex["stop_time_in_force"] not in ("GTC", "DAY"):
        raise ValueError("executor.stop_time_in_force は GTC / DAY")
    if ex["on_stop_failure"] not in ("flatten", "halt"):
        raise ValueError("executor.on_stop_failure は flatten / halt")
    if data["backtest"]["market_cap_proxy"] not in ("current_shares", "none"):
        raise ValueError("backtest.market_cap_proxy は current_shares / none")
    return Config(data)
