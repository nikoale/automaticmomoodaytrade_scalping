"""設定ファイル (YAML) の読み込み。"""
from __future__ import annotations

import os
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from datetime import time
from pathlib import Path
from typing import Any

import yaml

MODES = ("backtest", "paper", "simulate", "live")


@dataclass
class MoomooConfig:
    host: str = "127.0.0.1"
    port: int = 11111
    security_firm: str = "FUTUJP"        # moomoo証券(日本) の口座は FUTUJP
    trd_market: str = "US"               # 米国株のみ対応
    trade_password_env: str = "MOOMOO_TRADE_PASSWORD"  # 取引パスワードは環境変数から読む
    acc_id: int = 0


@dataclass
class SessionConfig:
    timezone: str = "America/New_York"
    # 取引時間帯 (米国東部時間)。終了 <= 開始 のものは日付をまたぐ (例: オーバーナイト 20:00〜04:00)
    sessions: list[list[str]] = field(default_factory=lambda: [["09:30", "16:00"]])
    no_entry_first_minutes: int = 5        # 寄り直後 N 分は新規エントリーしない
    no_entry_last_minutes: int = 15        # 各セッション終了 N 分前から新規エントリーしない
    flatten_before_close_minutes: int = 5  # 引け N 分前に全決済 (オーバーナイトしない)
    flatten_each_session: bool = True      # 時間帯が複数あるとき、各時間帯の終わりでも決済する
    # 米国株の時間外取引: RTH(通常のみ) / ETH(+プレ・アフター) / ALL(+オーバーナイト) / OVERNIGHT
    us_session: str = "RTH"
    # 「1 日」の区切り時刻。VWAP・ORB・1 日の損失上限がこの時刻でリセットされる。
    # 米国株のオーバーナイト (20:00〜翌4:00 ET) を含める場合は "20:00" にすると深夜 0 時でリセットされない
    day_rollover: str = "00:00"

    def parsed_sessions(self) -> list[tuple[time, time]]:
        out = []
        for start, end in self.sessions:
            out.append((_parse_time(start), _parse_time(end)))
        return out


@dataclass
class StrategyConfig:
    name: str = "ema_vwap"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExitConfig:
    stop_atr: float = 1.0        # 損切り幅 = ATR × stop_atr
    target_atr: float = 1.5      # 利確幅 = ATR × target_atr
    trail_atr: float = 0.0       # トレーリングストップ幅 (0 で無効)
    breakeven_atr: float = 0.0   # 含み益が ATR × これ を超えたらストップを建値へ (0 で無効)
    max_hold_bars: int = 30      # 時間切れ決済までの最大保有本数
    min_stop_ticks: int = 2      # 損切り幅の下限 (呼値単位)
    min_atr_ticks: float = 3.0   # ATR が呼値 × これ 未満の銘柄・時間帯は値幅不足として見送る


@dataclass
class RiskConfig:
    account_size: float = 10_000.0         # USD
    risk_per_trade: float = 0.003          # 1 トレードの許容損失 (口座比)
    max_position_value: float = 5_000.0    # 1 ポジションの最大建玉金額
    max_open_positions: int = 1
    lot_size: float = 1                    # 売買単位 (米国株は 1 株)
    max_daily_loss: float = 100.0          # 当日損失がこれに達したら当日は停止
    max_trades_per_day: int = 30
    max_consecutive_losses: int = 4        # 連敗でその日は停止
    cooldown_bars_after_loss: int = 3      # 負けトレード後に休む本数
    allow_short: bool = False              # 空売り (信用口座が必要)


@dataclass
class ExecutionConfig:
    tick_size: Any = "auto"         # "auto" なら 1 ドル以上 0.01 / 未満 0.0001
    slippage_ticks: float = 1.0     # バックテスト / paper の想定スリッページ
    commission_per_order: float = 0.0
    commission_rate: float = 0.0    # 約定代金に対する手数料率
    limit_offset_ticks: int = 1     # 実発注時、最良気配から何ティック不利側に指値を置くか
    order_timeout_sec: float = 5.0  # この秒数で約定しなければ取消
    use_market_orders: bool = False # True で成行 (スリッページに注意)


@dataclass
class AutoSymbolConfig:
    """銘柄の自動選定。enabled なら起動時に候補から上位 count 銘柄を選び symbols を置き換える。"""
    enabled: bool = False
    count: int = 3
    universe: list[str] = field(default_factory=list)  # 空なら組み込みの候補リスト
    min_price: float = 0.0
    max_price: float = 0.0          # 0 = 上限なし
    min_turnover: float = 0.0       # 当日の売買代金の下限 (USD)
    min_amplitude_pct: float = 1.0  # 当日値幅 % の下限
    max_spread_pct: float = 0.1     # スプレッド % の上限 (気配が取れない場合は判定しない)


@dataclass
class Config:
    mode: str = "paper"
    symbols: list[str] = field(default_factory=lambda: ["US.AAPL"])
    bar_minutes: int = 1
    warmup_bars: int = 100          # live 開始時に取得する過去足の本数
    log_dir: str = "logs"
    moomoo: MoomooConfig = field(default_factory=MoomooConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    auto_symbols: AutoSymbolConfig = field(default_factory=AutoSymbolConfig)

    @property
    def market(self) -> str:
        return self.moomoo.trd_market.upper()

    def trade_password(self) -> str | None:
        return os.environ.get(self.moomoo.trade_password_env) or None

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.market != "US":
            raise ValueError(f"trd_market={self.market} は未対応です。本ボットは米国株 (US) 専用です")
        if self.session.us_session.upper() not in ("RTH", "ETH", "ALL", "OVERNIGHT"):
            raise ValueError("session.us_session は RTH / ETH / ALL / OVERNIGHT のいずれか")
        if self.bar_minutes not in (1, 3, 5, 15):
            raise ValueError("bar_minutes は 1/3/5/15 のいずれか")
        if self.auto_symbols.enabled and not 1 <= self.auto_symbols.count <= 20:
            raise ValueError("auto_symbols.count は 1〜20")
        for s in self.symbols + [u.upper() for u in self.auto_symbols.universe]:
            if not s.upper().startswith(self.market + "."):
                raise ValueError(f"銘柄 {s} は米国株のコードではありません (例: US.AAPL)")


def _parse_time(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _build(cls, data: dict | None):
    data = data or {}
    kwargs = {}
    known = {f.name: f for f in fields(cls)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"{cls.__name__}: 不明な設定キー {key!r}")
        f = known[key]
        sub = f.default_factory if f.default_factory is not MISSING else None
        if sub is not None and is_dataclass(sub) and isinstance(value, dict):
            kwargs[key] = _build(sub, value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Config:
    data: dict = {}
    if path:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    if overrides:
        data.update({k: v for k, v in overrides.items() if v is not None})
    cfg = _build(Config, data)
    cfg.validate()
    return cfg
