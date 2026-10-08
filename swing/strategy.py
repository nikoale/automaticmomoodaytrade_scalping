"""フェーズ 2: 20 日高値ブレイクのトレンドフォロー戦略 (判定ルールだけ。発注・資金管理は別モジュール)。

判定はすべて日足の終値ベース、執行は翌営業日の寄り付き。
例外: 損切りは証券会社サーバー側の逆指値なので、日中に安値が損切り価格に触れた時点で約定する
(寄り付きで既に下回っていれば寄り付き値)。

エントリー : 監視リスト銘柄の終値 > 前日までの 20 日高値 かつ 出来高 >= 前日までの 20 日平均 × 1.5
初期損切り : エントリー価格 − ATR(14) × 2
トレーリング: 含み益 (終値 − エントリー価格) が エントリー時 ATR × 2 を超えたら開始し、以後毎日
              損切り = max(今の損切り, 20 日移動平均, 建玉後の最高値 − 当日 ATR × 2)   ※下げない
時間切れ   : 保有 20 営業日以上で 含み益 < エントリー時 ATR × 1 なら手仕舞い
決算       : 決算日の 2 営業日前の寄り付きで手仕舞い
指数フィルター: SPY 終値 < 200 日線 の日は全ポジションを翌寄り付きで手仕舞い (新規も停止)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date


@dataclass
class Position:
    symbol: str
    shares: float
    entry_date: date
    entry_idx: int            # バックテスト: 営業日の通し番号 / ライブ: 使わない場合は 0
    entry_price: float
    entry_atr: float
    stop: float
    highest: float            # 建玉後の最高値 (トレーリング用)
    trailing: bool = False
    entry_cost: float = 0.0   # 手数料・両替コスト込みの購入総額 (USD)
    rank: int = 0             # エントリー時の監視リスト順位

    def held_days(self, i: int) -> int:
        return i - self.entry_idx


def _ok(*xs) -> bool:
    return all(x is not None and not (isinstance(x, float) and math.isnan(x)) for x in xs)


def entry_signal(close, high_prev, volume, avg_vol_prev, cfg) -> bool:
    """終値が前日までの N 日高値を上抜け、かつ出来高が平均の 1.5 倍以上。"""
    if not _ok(close, high_prev, volume, avg_vol_prev) or avg_vol_prev <= 0:
        return False
    st = cfg.strategy
    return close > high_prev and volume >= avg_vol_prev * st["breakout_volume_mult"]


def initial_stop(entry_price: float, atr: float, cfg) -> float:
    return entry_price - atr * cfg.strategy["initial_stop_atr"]


def update_stop(pos: Position, close, high, sma_trail, atr_now, cfg) -> float:
    """引け後に損切り価格を更新する (翌営業日から有効)。損切りは下げない。"""
    st = cfg.strategy
    if _ok(high):
        pos.highest = max(pos.highest, high)
    if not pos.trailing and _ok(close) and close - pos.entry_price > st["trail_trigger_atr"] * pos.entry_atr:
        pos.trailing = True
    if pos.trailing and _ok(atr_now):
        cands = [pos.highest - st["trail_atr"] * atr_now]
        if _ok(sma_trail):
            cands.append(sma_trail)
        pos.stop = max(pos.stop, max(cands))
    return pos.stop


def stop_fill(open_, low, stop: float) -> float | None:
    """その日に逆指値が約定するなら約定価格 (スリッページ前)。寄り付きで下回っていれば寄り付き値。"""
    if _ok(open_) and open_ <= stop:
        return float(open_)
    if _ok(low) and low <= stop:
        return float(stop)
    return None


def exit_reason(pos: Position, i: int, close, earnings_in_days: int | None, index_ok: bool, cfg) -> str | None:
    """引けの時点で、翌営業日の寄り付きに手仕舞うべきか。

    earnings_in_days: 今日の翌営業日から次回決算日までの営業日数 (= trading_days_between(今日, 決算日))
    """
    st = cfg.strategy
    if st["index_filter"] and not index_ok:
        return "index_filter"
    # 決算日 E の 2 営業日前 (E-2) の寄り付きで売る → 今日から E まで 3 営業日以下になったら翌日売る
    if earnings_in_days is not None and earnings_in_days <= st["earnings_exit_days_before"] + 1:
        return "earnings"
    if pos.held_days(i) >= st["time_exit_days"] and _ok(close) and \
            close - pos.entry_price < st["time_exit_min_gain_atr"] * pos.entry_atr:
        return "time_exit"
    return None
