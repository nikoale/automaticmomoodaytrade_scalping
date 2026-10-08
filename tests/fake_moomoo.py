"""moomoo SDK の最小限のフェイク (OpenD なしでテストするため)。"""
import types

import pandas as pd


class _E:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def build(fill_mode="full", positions=None, power=1e9):
    """fill_mode: full=即全約定 / partial=半分約定 / none=約定しない。positions: 口座の初期保有株。"""
    m = types.ModuleType("moomoo")
    m.RET_OK, m.RET_ERROR = 0, -1
    m.TrdEnv = _E(REAL="REAL", SIMULATE="SIMULATE")
    m.TrdMarket = _E(US="US")
    m.SecurityFirm = _E(FUTUJP="FUTUJP")
    m.TrdSide = _E(BUY="BUY", SELL="SELL")
    m.OrderType = _E(NORMAL="NORMAL", MARKET="MARKET", STOP="STOP")
    m.ModifyOrderOp = _E(CANCEL="CANCEL", NORMAL="NORMAL")
    m.TimeInForce = _E(DAY="DAY", GTC="GTC")
    m.Currency = _E(USD="USD")
    init_positions = {"US.TEST": 100} if positions is None else dict(positions)

    class Ctx:
        """模擬の口座。通常注文は fill_mode に従って約定し保有株に反映、STOP 注文は待機する。"""
        instances = []

        def __init__(self, **kw):
            self.kw = kw
            self.orders = {}        # oid -> dict(status, dealt, avg, code, side, qty, type, remark, stop)
            self.placed = []
            self.cancelled = []
            self.modified = []
            self.unlocked = None
            self.holdings = dict(init_positions)
            self.power = power
            Ctx.instances.append(self)

        def unlock_trade(self, pwd):
            self.unlocked = pwd
            return 0, None

        def _apply(self, code, side, qty):
            self.holdings[code] = self.holdings.get(code, 0) + (qty if side == "BUY" else -qty)

        def place_order(self, price, qty, code, trd_side, order_type="NORMAL", **kw):
            oid = str(len(self.placed) + 1)
            self.placed.append(dict(price=price, qty=qty, code=code, side=trd_side, order_type=order_type, **kw))
            o = {"code": code, "side": trd_side, "qty": qty, "type": order_type, "remark": kw.get("remark", ""),
                 "stop": kw.get("aux_price"), "status": "SUBMITTED", "dealt": 0, "avg": 0.0}
            if order_type != "STOP":
                if fill_mode == "full":
                    o.update(status="FILLED_ALL", dealt=qty, avg=price)
                elif fill_mode == "partial":
                    o.update(status="FILLED_PART", dealt=qty // 2, avg=price)
                if o["dealt"]:
                    self._apply(code, trd_side, o["dealt"])
            self.orders[oid] = o
            return 0, pd.DataFrame([{"order_id": oid}])

        def trigger_stop(self, oid, price):
            """テスト用: 逆指値が約定したことにする。"""
            o = self.orders[oid]
            o.update(status="FILLED_ALL", dealt=o["qty"], avg=price)
            self._apply(o["code"], o["side"], o["qty"])

        def _row(self, oid, o):
            return {"order_id": oid, "code": o["code"], "order_status": o["status"], "dealt_qty": o["dealt"],
                    "dealt_avg_price": o["avg"], "remark": o["remark"], "qty": o["qty"]}

        def order_list_query(self, order_id="", **kw):
            if order_id:
                return 0, pd.DataFrame([self._row(order_id, self.orders[order_id])])
            return 0, pd.DataFrame([self._row(k, o) for k, o in self.orders.items()])

        def modify_order(self, op, order_id, qty, price, **kw):
            o = self.orders[order_id]
            if op == "CANCEL":
                self.cancelled.append(order_id)
                if o["status"] not in ("FILLED_ALL",):
                    o["status"] = "CANCELLED_PART" if o["dealt"] else "CANCELLED_ALL"
            else:
                self.modified.append((order_id, qty, price, kw.get("aux_price")))
                o.update(qty=qty, stop=kw.get("aux_price"))
            return 0, None

        def position_list_query(self, **kw):
            rows = [{"code": c, "qty": q, "position_side": "LONG"} for c, q in self.holdings.items() if q]
            return 0, pd.DataFrame(rows, columns=["code", "qty", "position_side"])

        def accinfo_query(self, **kw):
            return 0, pd.DataFrame([{"cash": self.power, "power": self.power, "usd_net_cash_power": self.power,
                                     "total_assets": self.power}])

        def close(self):
            pass

    m.OpenSecTradeContext = Ctx

    m.Session = _E(RTH="RTH", ETH="ETH", ALL="ALL", OVERNIGHT="OVERNIGHT")
    m.KLType = _E(K_1M="K_1M", K_3M="K_3M", K_5M="K_5M", K_15M="K_15M")
    m.SubType = _E(K_1M="K_1M", K_3M="K_3M", K_5M="K_5M", K_15M="K_15M", QUOTE="QUOTE", ORDER_BOOK="ORDER_BOOK")
    m.AuType = _E(QFQ="QFQ")

    class _Handler:
        def on_recv_rsp(self, rsp):
            return 0, rsp

    m.CurKlineHandlerBase = m.StockQuoteHandlerBase = m.OrderBookHandlerBase = _Handler

    def _klines(n, start="2026-10-08 00:00:00"):
        t0 = pd.Timestamp(start)
        return pd.DataFrame([{"code": "US.NVDA", "time_key": str(t0 + pd.Timedelta(minutes=i)), "open": 100.0,
                              "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000} for i in range(n)])

    class QuoteCtx:
        instances = []

        amp_override: dict = {}      # テストで銘柄ごとの値幅を変える

        def __init__(self, **kw):
            self.calls = []
            self.subscribed = False
            self.unsubscribed = []
            QuoteCtx.instances.append(self)

        def set_handler(self, h):
            pass

        def get_market_snapshot(self, codes):
            self.calls.append(("snapshot",))
            rows = []
            for i, c in enumerate(codes):
                amp = QuoteCtx.amp_override.get(c, 0.5 + (i * 7 % 10) * 0.4)   # 銘柄ごとに値幅を変える
                rows.append({"code": c, "name": c, "lot_size": 1, "last_price": 100.0, "bid_price": 99.99,
                             "ask_price": 100.01, "high_price": 100 + amp / 2, "low_price": 100 - amp / 2,
                             "prev_close_price": 100.0, "turnover": 2e8 + i * 1e7, "volume_ratio": 1.0,
                             "amplitude": amp, "suspension": False})
            return 0, pd.DataFrame(rows)

        def unsubscribe(self, codes, subs, **kw):
            self.unsubscribed.extend(codes)
            return 0, None

        def subscribe(self, codes, subs, **kw):
            self.calls.append(("subscribe", kw.get("session")))
            self.subscribed = True
            return 0, None

        def get_cur_kline(self, code, n, ktype, autype):
            self.calls.append(("cur_kline",))
            if not self.subscribed:
                return -1, "please subscribe first"
            return 0, _klines(n)

        def request_history_kline(self, code, **kw):
            self.calls.append(("history", kw.get("session")))
            return 0, _klines(300, "2026-10-07 20:00:00"), None

        def get_global_state(self):
            return 0, {"qot_logined": True, "trd_logined": True, "market_us": "OVERNIGHT"}

        def get_user_info(self, *a, **kw):
            return 0, {"us_qot_right": "LV3"}

        def get_order_book(self, code, num=10):
            return 0, {"code": code, "Bid": [(99.99, 100, 1, {})], "Ask": [(100.01, 120, 1, {})]}

        def get_stock_basicinfo(self, market, stype):
            return 0, pd.DataFrame([{"code": "US.NVDA", "name": "NVIDIA", "lot_size": 1},
                                    {"code": "US.AAPL", "name": "Apple", "lot_size": 1}])

        def close(self):
            pass

    m.Market = _E(US="US")
    m.SecurityType = _E(STOCK="STOCK")
    m.OpenQuoteContext = QuoteCtx
    return m
