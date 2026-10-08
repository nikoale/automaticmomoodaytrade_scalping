"""フェーズ 3: moomoo OpenD の発注まわり (薄いラッパー)。

関数名・引数は公式 SDK moomoo-api 10.11 のソースで確認 (実機では未検証):
  place_order(price, qty, code, trd_side, order_type, trd_env, acc_id, remark, time_in_force, aux_price, ...)
      OrderType.MARKET = 成行 / OrderType.STOP = 逆指値 (成行。aux_price が発動価格) / TimeInForce.DAY・GTC
  modify_order(ModifyOrderOp.NORMAL | CANCEL, order_id, qty, price, trd_env, acc_id, aux_price=...)
  order_list_query(order_id, trd_env, acc_id, refresh_cache)        今日の注文 (と有効な注文)
  history_order_list_query(start, end, trd_env, acc_id)               過去の注文
  position_list_query / accinfo_query / get_acc_list / unlock_trade

本番口座 (REAL) は二重ロック:
  1. config の moomoo.allow_real が true
  2. 起動時に人が確認する (resolve_env に confirm を渡す。GUI からは REAL を選べない)
  この 2 つを通ったときだけ RealUnlock が作られ、Broker は RealUnlock なしでは REAL を開かない。
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta

log = logging.getLogger(__name__)

# 注文の状態 (SDK の OrderStatus の文字列)
ACTIVE = {"UNSUBMITTED", "WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART", "CANCELLING_PART",
          "CANCELLING_ALL"}
FILLED = {"FILLED_ALL"}
PARTIAL_DONE = {"CANCELLED_PART"}                  # 一部約定して残りは取消
DEAD = {"CANCELLED_ALL", "FAILED", "SUBMIT_FAILED", "DISABLED", "DELETED", "FILL_CANCELLED"}
UNKNOWN = {"TIMEOUT", "N/A", "NONE", ""}           # 結果が分からない → 安全側 (停止)

REAL_CONFIRM_PHRASE = "本番口座で発注する"


class BrokerError(RuntimeError):
    """OpenD との通信・注文の失敗。呼び出し側は安全側 (発注停止) に倒す。"""


class RealLocked(RuntimeError):
    """本番口座のロックが外れていない。"""


class RealUnlock:
    """resolve_env が二重ロックを確認したときだけ作る印。"""
    _key = object()

    def __init__(self, key):
        if key is not RealUnlock._key:
            raise RealLocked("RealUnlock は resolve_env からしか作れません")


def resolve_env(cfg, confirm=None) -> tuple[str, RealUnlock | None]:
    """使う口座 (SIMULATE / REAL) を決める。REAL は config の flag と起動時の確認の両方が必要。"""
    env = str(cfg["moomoo"]["trd_env"]).upper()
    if env == "SIMULATE":
        return "SIMULATE", None
    if env != "REAL":
        raise ValueError("moomoo.trd_env は SIMULATE / REAL")
    if cfg["moomoo"].get("allow_real") is not True:
        raise RealLocked("本番口座はロックされています (config の moomoo.allow_real が true ではありません)")
    if confirm is None or not confirm():
        raise RealLocked("本番口座はロックされています (起動時の確認がされませんでした)")
    log.warning("本番口座 (REAL) での発注が有効になりました")
    return "REAL", RealUnlock(RealUnlock._key)


def _num(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def to_code(sym: str) -> str:
    return sym if sym.startswith("US.") else f"US.{sym}"


def to_sym(code: str) -> str:
    return code[3:] if str(code).startswith("US.") else str(code)


class Broker:
    def __init__(self, cfg, env: str = "SIMULATE", unlock: RealUnlock | None = None, password: str | None = None,
                 ctx=None):
        import moomoo as mm
        if env not in ("SIMULATE", "REAL"):
            raise ValueError("env は SIMULATE / REAL")
        if env == "REAL" and not isinstance(unlock, RealUnlock):
            raise RealLocked("本番口座はロックされています (resolve_env の確認を通っていません)")
        self.mm, self.cfg, self.env_name = mm, cfg, env
        self.env = getattr(mm.TrdEnv, env)
        m = cfg["moomoo"]
        self.ctx = ctx or mm.OpenSecTradeContext(filter_trdmarket=mm.TrdMarket.US, host=m["host"], port=m["port"],
                                                 security_firm=getattr(mm.SecurityFirm, m["security_firm"]))
        try:
            self.acc_id = self._pick_account()
            if env == "REAL":
                if not password:
                    raise BrokerError("本番口座には取引パスワードが必要です")
                ret, data = self.ctx.unlock_trade(password=password)
                if ret != mm.RET_OK:
                    raise BrokerError(f"ロック解除できませんでした: {data}")
        except Exception:
            self.ctx.close()
            raise

    def close(self) -> None:
        self.ctx.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _ok(self, ret, data, what: str):
        if ret != self.mm.RET_OK:
            raise BrokerError(f"{what} 失敗: {data}")
        return data

    # ---------------------------------------------------------------- 口座
    def _pick_account(self) -> int:
        df = self._ok(*self.ctx.get_acc_list(), "口座一覧 (get_acc_list)")
        rows = [r.to_dict() for _, r in df.iterrows() if str(r.get("trd_env")) == str(self.env)]
        rows = [r for r in rows if "US" in str(r.get("trdmarket_auth", ""))] or rows
        if self.env_name == "SIMULATE":     # 模擬口座は株式用を優先 (オプション用・コンテスト用もある)
            rows = [r for r in rows if str(r.get("sim_acc_type", "STOCK")) in ("STOCK", "N/A", "STOCK_AND_OPTION")] or rows
        if not rows:
            raise BrokerError(f"{self.env_name} の米国株口座が見つかりません")
        want = int(self.cfg["moomoo"].get("acc_id") or 0)
        if want:
            hit = [r for r in rows if int(r["acc_id"]) == want]
            if not hit:
                raise BrokerError(f"moomoo.acc_id={want} の口座が {self.env_name} にありません")
            return want
        return int(rows[0]["acc_id"])

    def funds(self) -> dict:
        """USD 建ての資産。power = 買付余力、cash = 現金。為替は JPY 建ての総資産との比。"""
        mm = self.mm
        usd = self._ok(*self.ctx.accinfo_query(trd_env=self.env, acc_id=self.acc_id, refresh_cache=True,
                                               currency=mm.Currency.USD), "資産 (accinfo_query)")
        u = usd.iloc[0].to_dict() if len(usd) else {}
        fx = None
        try:
            ret, jpy = self.ctx.accinfo_query(trd_env=self.env, acc_id=self.acc_id, refresh_cache=True,
                                              currency=mm.Currency.JPY)
            if ret == mm.RET_OK and len(jpy):
                tj, tu = _num(jpy.iloc[0].get("total_assets")), _num(u.get("total_assets"))
                from .account import sane_fx
                fx = sane_fx(self.cfg, tj / tu) if tj and tu else None
        except Exception:  # noqa: BLE001 - 為替は補助情報
            fx = None
        return {"total_assets": _num(u.get("total_assets")), "cash": _num(u.get("cash")),
                "power": _num(u.get("power")), "market_val": _num(u.get("market_val")), "fx": fx}

    def positions(self) -> dict[str, dict]:
        mm = self.mm
        df = self._ok(*self.ctx.position_list_query(trd_env=self.env, acc_id=self.acc_id,
                                                    position_market=mm.TrdMarket.US, refresh_cache=True),
                      "保有株 (position_list_query)")
        out = {}
        for _, r in df.iterrows():
            q = _num(r.get("qty")) or 0.0
            if q:
                out[to_sym(r["code"])] = {"qty": q, "can_sell_qty": _num(r.get("can_sell_qty")),
                                          "cost": _num(r.get("average_cost")) or _num(r.get("cost_price"))}
        return out

    # ---------------------------------------------------------------- 注文の照会
    @staticmethod
    def _row(r) -> dict:
        d = r.to_dict() if hasattr(r, "to_dict") else dict(r)
        return {"order_id": str(d.get("order_id")), "symbol": to_sym(d.get("code", "")),
                "side": str(d.get("trd_side")), "type": str(d.get("order_type")),
                "status": str(d.get("order_status")), "qty": _num(d.get("qty")) or 0.0,
                "price": _num(d.get("price")), "aux_price": _num(d.get("aux_price")),
                "dealt_qty": _num(d.get("dealt_qty")) or 0.0, "dealt_avg_price": _num(d.get("dealt_avg_price")),
                "remark": str(d.get("remark") or ""), "err": str(d.get("last_err_msg") or ""),
                "time": str(d.get("updated_time") or d.get("create_time") or "")}

    def orders(self) -> list[dict]:
        """今日の注文と、まだ有効な注文 (GTC の逆指値など)。"""
        df = self._ok(*self.ctx.order_list_query(trd_env=self.env, acc_id=self.acc_id, refresh_cache=True),
                      "注文一覧 (order_list_query)")
        return [self._row(r) for _, r in df.iterrows()]

    def order(self, order_id: str, days: int = 40) -> dict | None:
        """1 件の注文。今日の一覧になければ過去の注文から探す。"""
        oid = str(order_id)
        for o in self.orders():
            if o["order_id"] == oid:
                return o
        end = date.today() + timedelta(days=1)
        ret, df = self.ctx.history_order_list_query(trd_env=self.env, acc_id=self.acc_id,
                                                    start=(end - timedelta(days=days)).isoformat(), end=end.isoformat())
        df = self._ok(ret, df, "過去の注文 (history_order_list_query)")
        for _, r in df.iterrows():
            if str(r.get("order_id")) == oid:
                return self._row(r)
        return None

    # ---------------------------------------------------------------- 発注
    def _place(self, **kw) -> dict:
        ret, df = self.ctx.place_order(trd_env=self.env, acc_id=self.acc_id, **kw)
        df = self._ok(ret, df, f"発注 ({kw.get('code')} {kw.get('trd_side')} {kw.get('order_type')})")
        if not len(df):
            raise BrokerError("発注の応答が空です (結果不明)")
        row = self._row(df.iloc[0])
        if not row["order_id"] or row["order_id"] in ("None", "nan"):
            raise BrokerError("発注の応答に注文番号がありません (結果不明)")
        return row

    def market(self, sym: str, side: str, qty: float, remark: str) -> dict:
        """成行 (当日有効)。寄り付き前に出した場合は次の寄り付きで約定する想定 (※要確認)。"""
        mm = self.mm
        log.info("[発注] 成行 %s %s %s株 (%s)", "買い" if side == "BUY" else "売り", sym, qty, remark)
        return self._place(price=0.0, qty=qty, code=to_code(sym), trd_side=getattr(mm.TrdSide, side),
                           order_type=mm.OrderType.MARKET, remark=remark, time_in_force=mm.TimeInForce.DAY)

    def limit(self, sym: str, side: str, qty: float, price: float, remark: str) -> dict:
        mm = self.mm
        log.info("[発注] 指値 %s %s %s株 @%.2f (%s)", side, sym, qty, price, remark)
        return self._place(price=round(price, 2), qty=qty, code=to_code(sym), trd_side=getattr(mm.TrdSide, side),
                           order_type=mm.OrderType.NORMAL, remark=remark, time_in_force=mm.TimeInForce.DAY)

    def stop(self, sym: str, qty: float, stop_price: float, remark: str) -> dict:
        """証券会社サーバー側の逆指値 (成行) の売り。取消まで有効 (GTC)。"""
        mm = self.mm
        tif = getattr(mm.TimeInForce, self.cfg["executor"]["stop_time_in_force"])
        px = round(stop_price, 2)
        log.info("[発注] 逆指値 売り %s %s株 発動 %.2f (%s)", sym, qty, px, remark)
        return self._place(price=px, qty=qty, code=to_code(sym), trd_side=mm.TrdSide.SELL,
                           order_type=mm.OrderType.STOP, aux_price=px, remark=remark, time_in_force=tif)

    def modify_stop(self, order_id: str, qty: float, stop_price: float) -> None:
        mm = self.mm
        px = round(stop_price, 2)
        log.info("[訂正] 逆指値 %s の発動価格 → %.2f", order_id, px)
        self._ok(*self.ctx.modify_order(mm.ModifyOrderOp.NORMAL, str(order_id), qty, px, trd_env=self.env,
                                        acc_id=self.acc_id, aux_price=px), f"逆指値の訂正 ({order_id})")

    def modify_limit(self, order_id: str, qty: float, price: float) -> None:
        mm = self.mm
        self._ok(*self.ctx.modify_order(mm.ModifyOrderOp.NORMAL, str(order_id), qty, round(price, 2),
                                        trd_env=self.env, acc_id=self.acc_id), f"指値の訂正 ({order_id})")

    def cancel(self, order_id: str) -> None:
        mm = self.mm
        log.info("[取消] 注文 %s", order_id)
        self._ok(*self.ctx.modify_order(mm.ModifyOrderOp.CANCEL, str(order_id), 0, 0, trd_env=self.env,
                                        acc_id=self.acc_id), f"取消 ({order_id})")
