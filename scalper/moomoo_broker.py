"""moomoo OpenAPI (OpenD 経由) への実発注。

- mode: simulate → moomoo の模擬口座 (TrdEnv.SIMULATE。米国株のみ対応)
- mode: live     → 実口座 (TrdEnv.REAL)。取引パスワードでアンロックが必要

スキャルピングでは成行より「最良気配から数ティック不利側の指値」(実質的な即時約定指値) が安全。
order_timeout_sec 以内に約定しなければ取り消し、約定済み数量だけを返す。
"""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime
from typing import Callable

from .broker import Broker
from .config import Config
from .market import round_to_tick, tick_size
from .models import Fill, Side

log = logging.getLogger(__name__)

_FINAL_STATUSES = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "SUBMIT_FAILED", "DELETED",
                   "DISABLED"}


def _mm():
    try:
        import moomoo  # noqa: F401
    except ImportError as e:  # pragma: no cover - 環境依存
        raise SystemExit("moomoo-api が見つかりません: pip install moomoo-api") from e
    return moomoo


class MoomooBroker(Broker):
    def __init__(self, cfg: Config, best_quote: Callable[[str], tuple[float | None, float | None]] | None = None):
        mm = _mm()
        self.mm = mm
        self.cfg = cfg
        self.best_quote = best_quote or (lambda code: (None, None))
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
                raise SystemExit(f"実口座モードでは環境変数 {cfg.moomoo.trade_password_env} に取引パスワードを設定してください")
            ret, data = self.ctx.unlock_trade(pwd)
            if ret != mm.RET_OK:
                self.ctx.close()
                raise SystemExit(f"unlock_trade 失敗: {data}")
        self._order_kw = {"trd_env": self.env, "acc_id": cfg.moomoo.acc_id}
        if cfg.market == "JP":
            self._order_kw["jp_acc_type"] = getattr(mm.SubAccType, cfg.moomoo.jp_acc_type)
        us_session = cfg.session.us_session.upper()
        if cfg.market == "US" and us_session != "RTH":
            # 時間外取引の注文。※実口座・模擬口座での挙動は未検証。まず paper で確認を
            self._order_kw["session"] = getattr(mm.Session, us_session)
            self._order_kw["fill_outside_rth"] = us_session in ("ETH", "ALL")
        log.info("MoomooBroker ready: env=%s market=%s firm=%s", self.env, cfg.market, cfg.moomoo.security_firm)

    # ------------------------------------------------------------------ queries
    def positions(self) -> dict[str, int]:
        ret, df = self.ctx.position_list_query(trd_env=self.env, acc_id=self.cfg.moomoo.acc_id)
        if ret != self.mm.RET_OK:
            raise RuntimeError(f"position_list_query failed: {df}")
        out: dict[str, int] = {}
        for _, row in df.iterrows():
            qty = int(row["qty"])
            if str(row.get("position_side", "LONG")).upper() == "SHORT":
                qty = -qty
            if qty:
                out[row["code"]] = out.get(row["code"], 0) + qty
        return out

    def account_summary(self) -> dict:
        ret, df = self.ctx.accinfo_query(trd_env=self.env, acc_id=self.cfg.moomoo.acc_id)
        if ret != self.mm.RET_OK or df.empty:
            return {}
        return df.iloc[0].to_dict()

    # ------------------------------------------------------------------ orders
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
        trd_side = mm.TrdSide.BUY if side == Side.BUY else mm.TrdSide.SELL
        if ex.use_market_orders:
            order_type, price = mm.OrderType.MARKET, ref_price
        else:
            order_type, price = mm.OrderType.NORMAL, self._limit_price(code, side, ref_price)
        ret, data = self.ctx.place_order(price=price, qty=qty, code=code, trd_side=trd_side,
                                         order_type=order_type, remark=f"scalper:{reason}"[:60],
                                         **self._order_kw)
        if ret != mm.RET_OK:
            log.error("place_order failed (%s %s %g @ %s): %s", code, side.value, qty, price, data)
            return None
        order_id = str(data["order_id"].iloc[0])
        log.info("order %s placed: %s %s %g @ %s (%s)", order_id, code, side.value, qty, price, reason)

        dealt_qty, avg_price, status = self._wait(order_id, ex.order_timeout_sec)
        if status not in _FINAL_STATUSES:
            log.info("order %s not filled in %.1fs (status=%s, dealt=%d) -> cancel", order_id,
                     ex.order_timeout_sec, status, dealt_qty)
            self.ctx.modify_order(mm.ModifyOrderOp.CANCEL, order_id, 0, 0, **{
                k: v for k, v in self._order_kw.items() if k in ("trd_env", "acc_id")})
            dealt_qty, avg_price, status = self._wait(order_id, 3.0)
        if dealt_qty <= 0:
            return None
        return Fill(side, dealt_qty, avg_price or price, t,
                    self.commission(avg_price or price, dealt_qty, ex))

    def _wait(self, order_id: str, timeout: float) -> tuple[int, float, str]:
        mm = self.mm
        deadline = _time.monotonic() + timeout
        dealt, avg, status = 0, 0.0, "UNKNOWN"
        while True:
            ret, df = self.ctx.order_list_query(order_id=order_id, trd_env=self.env,
                                                acc_id=self.cfg.moomoo.acc_id)
            if ret == mm.RET_OK and not df.empty:
                row = df.iloc[0]
                dealt = int(float(row["dealt_qty"]))
                avg = float(row["dealt_avg_price"] or 0.0)
                status = str(row["order_status"])
                if status in _FINAL_STATUSES:
                    return dealt, avg, status
            if _time.monotonic() >= deadline:
                return dealt, avg, status
            _time.sleep(0.2)

    def close(self) -> None:
        self.ctx.close()
