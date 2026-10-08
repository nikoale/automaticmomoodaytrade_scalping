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
    return m
