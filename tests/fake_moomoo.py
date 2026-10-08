"""moomoo SDK の最小限のフェイク (OpenD なしでテストするため)。"""
import types

import pandas as pd


class _E:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def build(fill_mode="full"):
    m = types.ModuleType("moomoo")
    m.RET_OK, m.RET_ERROR = 0, -1
    m.TrdEnv = _E(REAL="REAL", SIMULATE="SIMULATE")
    m.TrdMarket = _E(JP="JP", US="US")
    m.SecurityFirm = _E(FUTUJP="FUTUJP")
    m.SubAccType = _E(JP_TOKUTEI="JP_TOKUTEI", JP_GENERAL="JP_GENERAL")
    m.TrdSide = _E(BUY="BUY", SELL="SELL")
    m.OrderType = _E(NORMAL="NORMAL", MARKET="MARKET")
    m.ModifyOrderOp = _E(CANCEL="CANCEL")

    class Ctx:
        instances = []

        def __init__(self, **kw):
            self.kw = kw
            self.orders = {}
            self.placed = []
            self.cancelled = []
            self.unlocked = None
            Ctx.instances.append(self)

        def unlock_trade(self, pwd):
            self.unlocked = pwd
            return 0, None

        def place_order(self, price, qty, code, trd_side, **kw):
            oid = str(len(self.placed) + 1)
            self.placed.append(dict(price=price, qty=qty, code=code, side=trd_side, **kw))
            if fill_mode == "full":
                self.orders[oid] = ("FILLED_ALL", qty, price)
            elif fill_mode == "partial":
                self.orders[oid] = ("FILLED_PART", qty // 2, price)
            else:
                self.orders[oid] = ("SUBMITTED", 0, 0.0)
            return 0, pd.DataFrame([{"order_id": oid}])

        def order_list_query(self, order_id="", **kw):
            st, dq, px = self.orders[order_id]
            return 0, pd.DataFrame([{"order_status": st, "dealt_qty": dq, "dealt_avg_price": px}])

        def modify_order(self, op, order_id, qty, price, **kw):
            self.cancelled.append(order_id)
            st, dq, px = self.orders[order_id]
            self.orders[order_id] = ("CANCELLED_PART" if dq else "CANCELLED_ALL", dq, px)
            return 0, None

        def position_list_query(self, **kw):
            return 0, pd.DataFrame([{"code": "JP.7203", "qty": 100, "position_side": "LONG"}])

        def accinfo_query(self, **kw):
            return 0, pd.DataFrame([{"cash": 1}])

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

        def __init__(self, **kw):
            self.calls = []
            self.subscribed = False
            QuoteCtx.instances.append(self)

        def set_handler(self, h):
            pass

        def get_market_snapshot(self, codes):
            self.calls.append(("snapshot",))
            return 0, pd.DataFrame([{"code": c, "name": c, "lot_size": 1, "last_price": 100.0, "bid_price": 99.99,
                                     "ask_price": 100.01} for c in codes])

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

        def close(self):
            pass

    m.OpenQuoteContext = QuoteCtx
    return m
