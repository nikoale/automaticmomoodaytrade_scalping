"""moomoo OpenAPI (OpenD 経由) への実発注。

- mode: simulate → moomoo の模擬口座 (TrdEnv.SIMULATE)
- mode: live     → 実口座 (TrdEnv.REAL)。取引パスワードでアンロックが必要

スキャルピングでは成行より「最良気配から数ティック不利側の指値」(実質的な即時約定指値) が安全。
order_timeout_sec 以内に約定しなければ取り消し、約定済み数量だけを返す。

保護ストップ: 建玉したら口座側にも逆指値 (STOP) の売り注文を置く。Mac のスリープや回線切れで
ボットが止まっても、口座側で損切りされる。ボットが決済するときは先に逆指値を取り消す。
"""
from __future__ import annotations

import logging
import time as _time
from collections import deque
from datetime import datetime
from typing import Callable

from .broker import Broker
from .config import Config
from .market import round_to_tick, tick_size
from .models import Fill, Side

log = logging.getLogger(__name__)

_FINAL_STATUSES = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "SUBMIT_FAILED", "DELETED",
                   "DISABLED", "FILL_CANCELLED"}
_OPEN_STATUSES = {"UNSUBMITTED", "WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART"}
REMARK_PREFIX = "scalper:"


def _mm():
    try:
        import moomoo  # noqa: F401
    except ImportError as e:  # pragma: no cover - 環境依存
        raise SystemExit("moomoo-api が見つかりません: pip install moomoo-api") from e
    return moomoo


class MoomooBroker(Broker):
    def __init__(self, cfg: Config, best_quote: Callable[[str], tuple[float | None, float | None]] | None = None,
                 on_halt: Callable[[str], None] | None = None):
        mm = _mm()
        self.mm = mm
        self.cfg = cfg
        self.best_quote = best_quote or (lambda code: (None, None))
        self.on_halt = on_halt or (lambda reason: None)
        self.env = mm.TrdEnv.SIMULATE if cfg.mode == "simulate" else mm.TrdEnv.REAL
        self.ctx = mm.OpenSecTradeContext(
            filter_trdmarket=getattr(mm.TrdMarket, cfg.market),
            host=cfg.moomoo.host, port=cfg.moomoo.port,
            security_firm=getattr(mm.SecurityFirm, cfg.moomoo.security_firm),
        )
        if self.env == mm.TrdEnv.REAL:
            pwd = cfg.trade_password()
            if not pwd:
                self.ctx.close()
                raise SystemExit("実口座モードでは取引パスワードが必要です")
            ret, data = self.ctx.unlock_trade(pwd)
            if ret != mm.RET_OK:
                self.ctx.close()
                raise SystemExit(f"取引のロック解除 (unlock_trade) に失敗しました: {data}")
        self._acc = {"trd_env": self.env, "acc_id": cfg.moomoo.acc_id}
        self._order_kw = dict(self._acc)
        us_session = cfg.session.us_session.upper()
        if us_session != "RTH":
            # 時間外取引の注文。※実口座・模擬口座での挙動は未検証。まず paper で確認を
            self._order_kw["session"] = getattr(mm.Session, us_session)
            self._order_kw["fill_outside_rth"] = us_session in ("ETH", "ALL")
        self._order_times: deque[float] = deque()
        self._halted = False
        self._bp_cache: tuple[float, float | None] = (0.0, None)
        log.info("MoomooBroker ready: env=%s market=%s firm=%s", self.env, cfg.market, cfg.moomoo.security_firm)

    # ------------------------------------------------------------------ queries
    def positions(self, refresh: bool = False) -> dict[str, float]:
        ret, df = self.ctx.position_list_query(refresh_cache=refresh, **self._acc)
        if ret != self.mm.RET_OK:
            raise RuntimeError(f"position_list_query failed: {df}")
        out: dict[str, float] = {}
        for _, row in df.iterrows():
            qty = float(row["qty"])
            if str(row.get("position_side", "LONG")).upper() == "SHORT":
                qty = -qty
            if qty:
                out[row["code"]] = out.get(row["code"], 0) + qty
        return out

    def account_summary(self) -> dict:
        ret, df = self.ctx.accinfo_query(currency=self.mm.Currency.USD, **self._acc)
        if ret != self.mm.RET_OK or df.empty:
            return {}
        return df.iloc[0].to_dict()

    def buying_power(self) -> float | None:
        """買付余力 (USD)。API 制限を避けるため 30 秒キャッシュ。"""
        t, v = self._bp_cache
        if _time.monotonic() - t < 30 and v is not None:
            return v
        info = self.account_summary()
        vals = [info.get(k) for k in ("usd_net_cash_power", "power")]
        nums = [float(x) for x in vals if x not in (None, "N/A") and str(x) != "nan"]
        v = min(nums) if nums else None
        self._bp_cache = (_time.monotonic(), v)
        return v

    def open_bot_orders(self) -> list[dict]:
        """ボットが出して残っている注文 (remark が scalper: で始まるもの)。"""
        ret, df = self.ctx.order_list_query(**self._acc)
        if ret != self.mm.RET_OK:
            raise RuntimeError(f"order_list_query failed: {df}")
        out = []
        for _, r in df.iterrows():
            if str(r.get("order_status")) in _OPEN_STATUSES and str(r.get("remark", "")).startswith(REMARK_PREFIX):
                out.append({"order_id": str(r["order_id"]), "code": r["code"], "status": str(r["order_status"]),
                            "remark": r.get("remark"), "qty": float(r.get("qty", 0))})
        return out

    def cancel(self, order_id: str) -> tuple[float, float, str]:
        self.ctx.modify_order(self.mm.ModifyOrderOp.CANCEL, order_id, 0, 0, **self._acc)
        return self._wait(order_id, 3.0)

    # ------------------------------------------------------------------ orders
    def _rate_ok(self) -> bool:
        """暴走防止: 1 分間の発注数が上限を超えたら停止する。"""
        if self._halted:
            return False
        now = _time.monotonic()
        while self._order_times and now - self._order_times[0] > 60:
            self._order_times.popleft()
        if len(self._order_times) >= self.cfg.execution.max_orders_per_minute:
            self._halted = True
            reason = f"発注回数が 1 分間に {self.cfg.execution.max_orders_per_minute} 回を超えました (暴走防止)"
            log.error(reason)
            self.on_halt(reason)
            return False
        self._order_times.append(now)
        return True

    def _limit_price(self, code: str, side: Side, ref_price: float) -> float:
        ex = self.cfg.execution
        bid, ask = self.best_quote(code)
        base = (ask if side == Side.BUY else bid) or ref_price
        tick = tick_size(base, self.cfg.market, ex.tick_size)
        if side == Side.BUY:
            return round_to_tick(base + ex.limit_offset_ticks * tick, tick, "up")
        return round_to_tick(base - ex.limit_offset_ticks * tick, tick, "down")

    def execute(self, code, side, qty, ref_price, t: datetime, reason="", exact=False):
        mm, ex = self.mm, self.cfg.execution
        if qty <= 0:
            return None
        # 決済 (exit) は暴走防止の上限に関係なく通す。新規だけ止める
        if reason.startswith("entry") and not self._rate_ok():
            return None
        trd_side = mm.TrdSide.BUY if side == Side.BUY else mm.TrdSide.SELL
        if ex.use_market_orders:
            order_type, price = mm.OrderType.MARKET, ref_price
        else:
            order_type, price = mm.OrderType.NORMAL, self._limit_price(code, side, ref_price)
        ret, data = self.ctx.place_order(price=price, qty=qty, code=code, trd_side=trd_side,
                                         order_type=order_type, remark=f"{REMARK_PREFIX}{reason}"[:60],
                                         **self._order_kw)
        if ret != mm.RET_OK:
            log.error("place_order failed (%s %s %g @ %s): %s", code, side.value, qty, price, data)
            return None
        order_id = str(data["order_id"].iloc[0])
        log.info("order %s placed: %s %s %g @ %s (%s)", order_id, code, side.value, qty, price, reason)

        dealt_qty, avg_price, status = self._wait(order_id, ex.order_timeout_sec)
        if status not in _FINAL_STATUSES:
            log.info("order %s not filled in %.1fs (status=%s, dealt=%g) -> cancel", order_id,
                     ex.order_timeout_sec, status, dealt_qty)
            dealt_qty, avg_price, status = self.cancel(order_id)
            if status not in _FINAL_STATUSES:
                # 取消の確認が取れない。後から約定する可能性があるので、照合 (reconcile) で検出する
                log.warning("order %s の取消を確認できません (status=%s)。口座との照合で確認します", order_id, status)
        if dealt_qty <= 0:
            return None
        return Fill(side, dealt_qty, avg_price or price, t, self.commission(avg_price or price, dealt_qty, ex))

    # ------------------------------------------------------------------ 保護ストップ
    def protect(self, code, qty, stop, is_long=True):
        if not self.cfg.execution.protective_stop or qty <= 0:
            return None
        mm = self.mm
        tick = tick_size(stop, self.cfg.market, self.cfg.execution.tick_size)
        stop = round_to_tick(stop, tick, "down" if is_long else "up")
        side = mm.TrdSide.SELL if is_long else mm.TrdSide.BUY
        ret, data = self.ctx.place_order(price=stop, qty=qty, code=code, trd_side=side,
                                         order_type=mm.OrderType.STOP, aux_price=stop,
                                         # GTC: ボットが止まって持ち越しになっても翌日以降も保護が残る
                                         time_in_force=mm.TimeInForce.GTC,
                                         remark=f"{REMARK_PREFIX}protect", **self._acc)
        if ret != mm.RET_OK:
            log.error("%s 保護ストップの発注に失敗 (ボット側の損切りのみで管理します): %s", code, data)
            return None
        oid = str(data["order_id"].iloc[0])
        log.info("%s 保護ストップ %s: %g 株 @ %s", code, oid, qty, stop)
        return oid

    def update_protect(self, order_id, code, qty, stop, is_long=True):
        if order_id is None:
            return None
        mm = self.mm
        tick = tick_size(stop, self.cfg.market, self.cfg.execution.tick_size)
        stop = round_to_tick(stop, tick, "down" if is_long else "up")
        ret, data = self.ctx.modify_order(mm.ModifyOrderOp.NORMAL, order_id, qty, stop, aux_price=stop,
                                          **self._acc)
        if ret == mm.RET_OK:
            log.info("%s 保護ストップ %s を %s に変更", code, order_id, stop)
            return order_id
        log.warning("%s 保護ストップの変更に失敗 (%s)。取り消して置き直します", code, data)
        dealt, _, _ = self.cancel(order_id)
        if dealt > 0:
            return order_id   # 既に約定していた → 照合で決済扱いになる
        return self.protect(code, qty, stop, is_long)

    def release_protect(self, order_id):
        if order_id is None:
            return 0.0, 0.0
        dealt, avg, status = self.cancel(order_id)
        if status not in _FINAL_STATUSES:
            log.warning("保護ストップ %s の取消を確認できません (status=%s)", order_id, status)
        return float(dealt), float(avg)

    def order_fill(self, order_id: str) -> tuple[float, float, str]:
        return self._wait(order_id, 0)

    def _wait(self, order_id: str, timeout: float) -> tuple[float, float, str]:
        mm = self.mm
        deadline = _time.monotonic() + timeout
        dealt, avg, status = 0.0, 0.0, "UNKNOWN"
        while True:
            ret, df = self.ctx.order_list_query(order_id=order_id, **self._acc)
            if ret == mm.RET_OK and not df.empty:
                row = df.iloc[0]
                dealt = float(row["dealt_qty"] or 0)
                avg = float(row["dealt_avg_price"] or 0.0)
                status = str(row["order_status"])
                if status in _FINAL_STATUSES:
                    return dealt, avg, status
            if _time.monotonic() >= deadline:
                return dealt, avg, status
            _time.sleep(0.2)

    def close(self) -> None:
        self.ctx.close()
