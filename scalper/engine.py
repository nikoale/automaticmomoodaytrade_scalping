"""1 銘柄ぶんの売買ロジック。バックテストとライブで共通。

処理の流れ (確定足ごと):
  1. (バックテストのみ) 足の高値/安値で損切り・利確に触れたか判定
  2. 戦略に足を渡してシグナル取得
  3. 保有中: トレーリング / 建値ストップ更新 → 時間切れ・引け前・反対シグナルで決済
  4. 未保有: 取引時間・リスク制限を確認して新規エントリー
ライブではティック (on_price) ごとにも損切り・利確を判定する。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from .broker import Broker
from .config import Config
from .market import TradingSessions, round_to_tick, tick_size
from .models import Bar, Position, Side, Signal, Trade
from .risk import RiskManager
from .strategies import Strategy

log = logging.getLogger(__name__)

EXIT_RETRY_SECONDS = 10


class SymbolEngine:
    def __init__(self, code: str, cfg: Config, strategy: Strategy, risk: RiskManager, broker: Broker,
                 sessions: TradingSessions | None = None, lot_size: float | None = None,
                 on_trade: Callable[[Trade], None] | None = None):
        self.code = code
        self.cfg = cfg
        self.strategy = strategy
        self.risk = risk
        self.broker = broker
        self.sessions = sessions or TradingSessions.from_config(cfg.session)
        self.strategy.day_offset = self.sessions.day_offset
        self.lot_size = lot_size or cfg.risk.lot_size
        self.on_trade = on_trade
        self.position: Position | None = None
        self.trades: list[Trade] = []
        self.enabled = True          # False にすると新規エントリーしない (ライブの安全装置)
        self.last_price: float | None = None
        self._exit_retry_at: datetime | None = None   # 決済失敗後の再試行時刻 (発注連打防止)

    # ------------------------------------------------------------------ helpers
    def _tick(self, price: float) -> float:
        return tick_size(price, self.cfg.market, self.cfg.execution.tick_size)

    @property
    def is_flat(self) -> bool:
        return self.position is None

    # ------------------------------------------------------------------ bar
    def warmup(self, bars: list[Bar]) -> None:
        """過去足で指標だけを温める (売買しない)。"""
        for b in bars:
            self.strategy.on_bar(b)

    def on_bar(self, bar: Bar, intrabar_exits: bool = True) -> None:
        self.risk.roll_day(bar.time - self.sessions.day_offset)
        self.risk.on_bar(self.code)
        self.last_price = bar.close

        if self.position is not None and intrabar_exits:
            self._check_intrabar(bar)

        signal = self.strategy.on_bar(bar)

        if self.position is not None:
            self._manage_open(bar, signal)
        elif signal in (Signal.LONG, Signal.SHORT):
            self._try_enter(bar, signal)

    def _check_intrabar(self, bar: Bar) -> None:
        """足の OHLC から損切り/利確を判定。両方に触れた足は保守的に損切り扱い。"""
        p = self.position
        if p.is_long:
            if bar.low <= p.stop:
                self._exit(min(p.stop, bar.open), bar.time, "stop")
            elif bar.high >= p.target:
                self._exit(max(p.target, bar.open), bar.time, "target", slippage=False)
        else:
            if bar.high >= p.stop:
                self._exit(max(p.stop, bar.open), bar.time, "stop")
            elif bar.low <= p.target:
                self._exit(min(p.target, bar.open), bar.time, "target", slippage=False)

    def _manage_open(self, bar: Bar, signal: Signal) -> None:
        p = self.position
        p.bars_held += 1
        p.best_price = max(p.best_price, bar.high) if p.is_long else min(p.best_price, bar.low)
        self._update_trailing(bar)

        if self.sessions.must_flatten(bar.time):
            self._exit(bar.close, bar.time, "session_end")
        elif self.risk.halted_reason and "loss" in self.risk.halted_reason:
            self._exit(bar.close, bar.time, "risk_halt")
        elif p.bars_held >= self.cfg.exits.max_hold_bars:
            self._exit(bar.close, bar.time, "time_stop")
        elif signal == Signal.EXIT or (signal == Signal.SHORT and p.is_long) or \
                (signal == Signal.LONG and not p.is_long):
            self._exit(bar.close, bar.time, "signal")
        else:
            # 足確定時点で既に損切りラインを割っていれば即決済 (ライブのフォールバック)
            self.check_price(bar.close, bar.time)

    def _update_trailing(self, bar: Bar) -> None:
        p, ex = self.position, self.cfg.exits
        atr = self.strategy.atr.value or p.atr
        tick = self._tick(p.entry_price)
        if p.is_long:
            new_stop = p.stop
            if ex.breakeven_atr > 0 and p.best_price - p.entry_price >= ex.breakeven_atr * p.atr:
                new_stop = max(new_stop, p.entry_price + tick)
            if ex.trail_atr > 0:
                new_stop = max(new_stop, round_to_tick(p.best_price - ex.trail_atr * atr, tick, "down"))
            if new_stop > p.stop:
                log.debug("%s trail stop %.4f -> %.4f", self.code, p.stop, new_stop)
                p.stop = new_stop
                self._sync_protect()
        else:
            new_stop = p.stop
            if ex.breakeven_atr > 0 and p.entry_price - p.best_price >= ex.breakeven_atr * p.atr:
                new_stop = min(new_stop, p.entry_price - tick)
            if ex.trail_atr > 0:
                new_stop = min(new_stop, round_to_tick(p.best_price + ex.trail_atr * atr, tick, "up"))
            if new_stop < p.stop:
                p.stop = new_stop
                self._sync_protect()

    # ------------------------------------------------------------------ tick (live)
    def check_price(self, price: float, t: datetime) -> None:
        """リアルタイム価格で損切り/利確を判定する。"""
        self.last_price = price
        p = self.position
        if p is None:
            return
        if p.is_long:
            if price <= p.stop:
                self._exit(price, t, "stop")
            elif price >= p.target:
                self._exit(price, t, "target")
        else:
            if price >= p.stop:
                self._exit(price, t, "stop")
            elif price <= p.target:
                self._exit(price, t, "target")

    def on_clock(self, t: datetime) -> None:
        """足が来なくても引け前には必ず手仕舞う (ライブ用タイマー)。"""
        if self.position is not None and self.last_price is not None and self.sessions.must_flatten(t):
            self._exit(self.last_price, t, "session_end")

    # ------------------------------------------------------------------ entry / exit
    def _try_enter(self, bar: Bar, signal: Signal) -> None:
        if not self.enabled:
            return
        if signal == Signal.SHORT and not self.cfg.risk.allow_short:
            return
        if not self.sessions.can_enter(bar.time):
            return
        ok, why = self.risk.can_open(self.code)
        if not ok:
            log.debug("%s skip entry: %s", self.code, why)
            return
        atr = self.strategy.atr.value
        if not atr or atr <= 0:
            return
        ex = self.cfg.exits
        price = bar.close
        tick = self._tick(price)
        if atr < ex.min_atr_ticks * tick:
            log.debug("%s skip entry: ATR %.4f too small vs tick %.4f", self.code, atr, tick)
            return
        is_long = signal == Signal.LONG
        sign = 1 if is_long else -1

        stop_dist = max(atr * ex.stop_atr, ex.min_stop_ticks * tick)
        hint = self.strategy.stop_hint()
        if hint is not None and (price - hint) * sign > 0:
            # 戦略提案の損切り (ORB のレンジ反対側など)。ATR 損切りの 2 倍までに制限
            stop_dist = min(max((price - hint) * sign, ex.min_stop_ticks * tick), 2 * atr * ex.stop_atr)
        target_dist = atr * ex.target_atr
        thint = self.strategy.target_hint()
        if thint is not None and (thint - price) * sign > tick:
            target_dist = thint - price if is_long else price - thint

        # 手数料負け防止: 利確幅が往復コスト (手数料 + スリッページ) の何倍あるか
        ec = self.cfg.execution
        round_trip_cost = 2 * price * ec.commission_rate + 2 * ec.slippage_ticks * tick
        if ex.min_reward_cost_ratio > 0 and target_dist < ex.min_reward_cost_ratio * round_trip_cost:
            log.debug("%s skip entry: 利確幅 %.4f < 往復コスト %.4f × %.1f", self.code, target_dist,
                      round_trip_cost, ex.min_reward_cost_ratio)
            return

        qty = self.risk.position_size(price, stop_dist, self.lot_size)
        bp = self.broker.buying_power()
        if bp is not None and qty > 0:
            # 買付余力の 95% まで (手数料・価格変動の余裕)
            lot = float(self.lot_size or 1)
            max_qty = int(bp * 0.95 / price / lot) * lot
            if max_qty < qty:
                log.info("%s 買付余力 %.0f USD に合わせて %g → %g 株", self.code, bp, qty, max_qty)
                qty = max_qty
        if qty <= 0:
            log.debug("%s size=0 (price=%.2f stop_dist=%.4f)", self.code, price, stop_dist)
            return

        fill = self.broker.execute(self.code, Side.BUY if is_long else Side.SELL, qty, price, bar.time,
                                   reason=f"entry_{signal.value.lower()}")
        if fill is None or fill.qty <= 0:
            log.info("%s entry not filled", self.code)
            return
        fp = fill.price
        if is_long:
            stop = round_to_tick(fp - stop_dist, tick, "down")
            target = round_to_tick(fp + target_dist, tick, "up")
        else:
            stop = round_to_tick(fp + stop_dist, tick, "up")
            target = round_to_tick(fp - target_dist, tick, "down")
        self.position = Position(qty=fill.qty * sign, entry_price=fp, entry_time=fill.time, stop=stop,
                                 target=target, best_price=fp, entry_commission=fill.commission, atr=atr,
                                 reason=signal.value)
        self.position.protect_id = self.broker.protect(self.code, fill.qty, stop, is_long)
        self.risk.on_open(self.code)
        log.info("%s ENTER %s %g @ %.4f stop=%.4f target=%.4f", self.code, signal.value, fill.qty, fp,
                 stop, target)

    def _sync_protect(self) -> None:
        """口座側の保護ストップを今の損切り価格・数量に合わせる。"""
        p = self.position
        if p is not None and p.protect_id is not None:
            p.protect_id = self.broker.update_protect(p.protect_id, self.code, abs(p.qty), p.stop, p.is_long)

    def _exit(self, ref_price: float, t: datetime, reason: str, slippage: bool = True) -> None:
        p = self.position
        if p is None:
            return
        if self._exit_retry_at is not None and t < self._exit_retry_at:
            return
        # 先に口座側の保護ストップを取り消す (二重に売らないため)。取消前に約定していたらそれを決済とする
        if p.protect_id is not None:
            dealt, avg = self.broker.release_protect(p.protect_id)
            p.protect_id = None
            if dealt > 0:
                comm = self.broker.commission(avg or p.stop, dealt, self.cfg.execution)
                self._record_exit(min(dealt, abs(p.qty)), avg or p.stop, comm, t, "protective_stop")
                if self.position is None:
                    return
                p = self.position
        side = Side.SELL if p.is_long else Side.BUY
        qty = abs(p.qty)
        # 利確は指値に当たった想定なのでシミュレーションではスリッページなし
        fill = self.broker.execute(self.code, side, qty, ref_price, t, reason=reason, exact=not slippage)
        if fill is None or fill.qty <= 0:
            log.warning("%s exit (%s) not filled; retry in %ds", self.code, reason, EXIT_RETRY_SECONDS)
            self._exit_retry_at = t + timedelta(seconds=EXIT_RETRY_SECONDS)
            p.protect_id = self.broker.protect(self.code, qty, p.stop, p.is_long)   # 保護を戻す
            return
        self._exit_retry_at = None
        self._record_exit(min(fill.qty, qty), fill.price, fill.commission, fill.time, reason)
        if self.position is not None:   # 一部だけ約定 → 残りに保護ストップを置き直す
            self.position.protect_id = self.broker.protect(self.code, abs(self.position.qty), self.position.stop,
                                                           self.position.is_long)

    def _record_exit(self, filled: float, price: float, commission: float, t: datetime, reason: str) -> None:
        """filled 株の決済を記録する。全部決済したらポジションを閉じる。"""
        p = self.position
        qty = abs(p.qty)
        sign = 1 if p.is_long else -1
        entry_comm = p.entry_commission * filled / qty
        pnl = (price - p.entry_price) * filled * sign - entry_comm - commission
        trade = Trade(self.code, "LONG" if p.is_long else "SHORT", filled, p.entry_time, p.entry_price,
                      t, price, pnl, reason, p.bars_held)
        self.trades.append(trade)
        log.info("%s EXIT %s %g @ %.4f pnl=%.2f (%s)", self.code, trade.direction, filled, price, pnl, reason)
        if self.on_trade:
            self.on_trade(trade)
        if filled < qty:
            p.qty = (qty - filled) * sign
            p.entry_commission -= entry_comm
            self.risk.daily_pnl += pnl
            return
        self.position = None
        self.risk.on_close(self.code, pnl)

    def on_external_close(self, qty: float, price: float, t: datetime, reason: str) -> None:
        """ボットの外で決済されていた分 (保護ストップの約定など) を記録する。"""
        if self.position is None or qty <= 0:
            return
        comm = self.broker.commission(price, qty, self.cfg.execution)
        self._record_exit(min(qty, abs(self.position.qty)), price, comm, t, reason)

    def force_flatten(self, t: datetime, reason: str = "manual") -> None:
        if self.position is not None and self.last_price is not None:
            self._exit(self.last_price, t, reason)
