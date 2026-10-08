"""フェーズ 3: 発注・リスク管理。日足の判断 (strategy.py) を証券会社の注文に変える。

1 日の流れ (日本時間。夏時間は calendar_us が自動で扱う):
  引け後の処理 run_close (朝 7:30 ごろ。米国の引けの後):
    1. 照合: 前回の注文の約定・逆指値の約定・保有株数を証券会社の記録と突き合わせる
    2. 建玉に逆指値がなければ入れる (入らなければ成行で売って停止 / config)
    3. 日足を更新 → 損切り価格を上げる (証券会社側の逆指値を訂正) → 手仕舞い判定 → 新規エントリー判定
    4. order_timing=reserve なら、翌寄り付きの成行をこの時点で予約する (受け付けられなければ寄り付き後の処理で出す)
  寄り付き後の処理 run_open (寄り付き直後):
    まだ出していない注文を出し、約定を待つ → 約定した建玉にすぐ逆指値を入れる → 約定しなければ取消して停止

安全側に倒す (発注停止 = halt。解除は人が resume するまで続く):
  OpenD に接続できない / 注文の結果が分からない / 未約定 / 二重発注の疑い / ボットの記録と保有株数が合わない /
  1 回の処理で注文が多すぎる
  停止中も、証券会社側に入っている逆指値はそのまま残るので、ボットが落ちても損切りは生きている。

ボット用の資金はボット自身の帳簿 (受渡 T+1 を含む) で管理し、証券会社の買付余力とも比べて小さい方を使う。
帳簿・建玉・注文は data.dir/trade/state_<SIMULATE|REAL>.json に保存する (金額を含むのでこのファイルだけに置く)。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from . import calendar_us, risk, strategy
from .broker import ACTIVE, DEAD, FILLED, PARTIAL_DONE, UNKNOWN, BrokerError
from .strategy import Position

log = logging.getLogger(__name__)

STATE_VERSION = 1


class Halted(RuntimeError):
    pass


def remark(d: date, sym: str, kind: str) -> str:
    """注文に付ける目印 (64 バイトまで)。同じ目印の注文が既にあれば出し直さない (二重発注の防止)。"""
    return f"swb:{d:%y%m%d}:{sym}:{kind}"


# ---------------------------------------------------------------- 保存する状態
def state_path(cfg, env: str) -> Path:
    return cfg.path(cfg["executor"]["state_dir"], f"state_{env}.json")


def new_state(env: str) -> dict:
    return {"version": STATE_VERSION, "env": env, "acc_id": None, "created": None, "capital_usd": None,
            "cash_settled": None, "pending_sales": [], "positions": {}, "orders": [], "cooldown": {},
            "week": {}, "last_equity": None, "halt": {"on": False, "reason": "", "time": None},
            "trades": [], "last_close": None, "last_open": None, "last_summary": None, "equity_history": []}


def load_state(cfg, env: str) -> dict:
    p = state_path(cfg, env)
    if not p.exists():
        return new_state(env)
    st = json.loads(p.read_text(encoding="utf-8"))
    if st.get("env") != env:
        raise RuntimeError(f"状態ファイルの口座が違います ({st.get('env')} ≠ {env})")
    return st


def save_state(cfg, env: str, st: dict) -> None:
    p = state_path(cfg, env)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)                    # 書き込み途中で落ちても壊れない


def pos_to_dict(p: Position, **extra) -> dict:
    d = asdict(p)
    d["entry_date"] = p.entry_date.isoformat()
    d.update(extra)
    return d


def pos_from_dict(d: dict) -> Position:
    keys = Position.__dataclass_fields__
    kw = {k: v for k, v in d.items() if k in keys}
    kw["entry_date"] = date.fromisoformat(d["entry_date"])
    return Position(**kw)


class RunLock:
    """同時に 2 つ動かない (画面とスケジューラーが同時に押したときなど)。"""

    def __init__(self, path: Path, stale_sec: int = 2 * 3600):
        self.path, self.stale = path, stale_sec

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > self.stale:
            self.path.unlink()
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RuntimeError("別の発注処理が実行中です (終わってからもう一度)") from None
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- 本体
class Executor:
    def __init__(self, cfg, broker, env: str, market_data=None, now=None, sleep=time.sleep):
        """broker: broker.Broker (テストでは偽物)。market_data: moomoo_data.MoomooData 相当 (日足・決算カレンダー)。"""
        self.cfg, self.broker, self.env = cfg, broker, env
        self.md = market_data
        self.now = now or (lambda: datetime.now(calendar_us.TOKYO))
        self.sleep = sleep
        self.ex = cfg["executor"]
        self.st = load_state(cfg, env)
        self.placed_this_run = 0

    # ---------------------------------------------------------------- 共通
    def save(self) -> None:
        save_state(self.cfg, self.env, self.st)

    @property
    def halted(self) -> bool:
        return bool(self.st["halt"]["on"])

    def halt(self, reason: str) -> None:
        if not self.halted:
            self.st["halt"] = {"on": True, "reason": reason, "time": self.now().isoformat(timespec="seconds")}
        log.error("[停止] 発注を止めました: %s (解除するまで新しい注文は出しません。証券会社側の逆指値は残ります)", reason)
        self.save()

    def resume(self) -> None:
        log.warning("[停止解除] %s", self.st["halt"].get("reason"))
        self.st["halt"] = {"on": False, "reason": "", "time": None}
        self.save()

    def reset(self) -> dict:
        """模擬口座だけ: ボットの有効な注文 (買い・売り・逆指値) を取り消し、記録を最初からにする。

        前の記録は state_SIMULATE.reset-<日時>.json に残す。約定済みの株は自動では売らないので、一覧を返す
        (moomoo アプリの模擬口座で売ってください)。
        """
        if self.env != "SIMULATE":
            raise RuntimeError("記録のリセットは模擬口座 (SIMULATE) だけです")
        ids = [(o["symbol"], o["order_id"]) for o in self.st["orders"] if o.get("order_id")]
        ids += [(sym, p["stop_order_id"]) for sym, p in self.st["positions"].items() if p.get("stop_order_id")]
        cancelled, filled = [], [{"symbol": s, "shares": p["shares"]} for s, p in self.st["positions"].items()]
        for sym, oid in ids:
            b = self.broker.order(oid)
            if b and b["status"] in ACTIVE:
                self.broker.cancel(oid)
                cancelled.append(f"{sym} (注文 {oid})")
            if b and b["dealt_qty"] > 0 and b["side"] == "BUY":
                filled.append({"symbol": sym, "shares": b["dealt_qty"]})
        p = state_path(self.cfg, self.env)
        if p.exists():
            os.replace(p, p.with_name(f"state_{self.env}.reset-{self.now():%Y%m%d-%H%M%S}.json"))
        self.st = new_state(self.env)
        log.warning("[リセット] 模擬口座の記録を最初からにしました。取り消した注文: %s", ", ".join(cancelled) or "なし")
        if filled:
            log.warning("[リセット] 約定済みの株が模擬口座に残っています (moomoo アプリで売ってください): %s",
                        ", ".join(f"{x['symbol']} {x['shares']:g}株" for x in filled))
        return {"cancelled": cancelled, "left_shares": filled}

    def _session_today(self) -> date:
        """今が取引時間中ならその日、それ以外は直近の引け済みの取引日。"""
        now = self.now()
        d = now.astimezone(calendar_us.NY).date()
        if calendar_us.is_trading_day(d):
            o, _ = calendar_us.session_times_jst(d)
            if now >= o:
                return d
        return calendar_us.last_completed_session(now)

    def _ledger(self) -> risk.SettlementLedger:
        led = risk.SettlementLedger(self.st["cash_settled"], self.cfg.account["settlement_days"])
        led.pending = [(date.fromisoformat(k), a) for k, a in self.st["pending_sales"]]
        return led

    def _save_ledger(self, led: risk.SettlementLedger) -> None:
        self.st["cash_settled"] = led.settled
        self.st["pending_sales"] = [[k.isoformat(), a] for k, a in led.pending]

    def max_capital_usd(self) -> float:
        """設定の運用資金をドルにした上限 (為替の範囲の下限で割った、いちばん大きく見積もった値)。"""
        return float(self.cfg.account["capital_jpy"]) / float(self.cfg.account["fx_sane_range"][0])

    def _init_capital(self) -> None:
        if self.st["capital_usd"] is not None:
            if self.st["capital_usd"] > self.max_capital_usd() * 1.001:
                raise Halted(f"ボットの資金の記録 ({self.st['capital_usd']:,.2f} ドル) が設定の運用資金 "
                             f"({self.cfg.account['capital_jpy']:,} 円) より大きすぎます。"
                             "「模擬口座の記録をリセット」を押してやり直してください")
            return
        from .account import effective_capital
        f = self.broker.funds()
        cap = effective_capital(self.cfg, f.get("total_assets"), f.get("fx"))
        if cap["usd"] > self.max_capital_usd() * 1.001:
            raise Halted(f"ボットの資金の計算がおかしいので始めません ({cap['usd']:,.2f} ドル・為替 {cap['fx']})")
        self.st.update({"acc_id": self.broker.acc_id, "created": self.now().isoformat(timespec="seconds"),
                        "capital_usd": cap["usd"], "cash_settled": cap["usd"]})
        log.info("ボットの資金を %.2f ドル (約 %.0f 円) で始めます (%s・為替 1 ドル = %.2f 円: %s)",
                 cap["usd"], cap["jpy"], cap["reason"], cap["fx"], cap["fx_source"])
        self.save()

    def _known_ids(self) -> set[str]:
        ids = {o["order_id"] for o in self.st["orders"] if o.get("order_id")}
        ids |= {p["stop_order_id"] for p in self.st["positions"].values() if p.get("stop_order_id")}
        return ids

    def _count_order(self) -> None:
        self.placed_this_run += 1
        if self.placed_this_run > self.ex["max_orders_per_run"]:
            raise Halted(f"1 回の処理での注文数が上限 ({self.ex['max_orders_per_run']}) を超えました (暴走防止)")

    def _submit(self, rmk: str, sym: str, side: str, place) -> dict:
        """二重発注を防いで注文を出す。同じ目印の注文が既にあればそれを使う。"""
        orders = self.broker.orders()
        same = [o for o in orders if o["remark"] == rmk and o["status"] not in DEAD]
        if len(same) > 1:
            raise Halted(f"二重発注の疑い: 目印 {rmk} の注文が {len(same)} 件あります")
        if same:
            log.warning("目印 %s の注文が既にあるので出し直しません (注文 %s)", rmk, same[0]["order_id"])
            return same[0]
        known = self._known_ids()
        other = [o for o in orders if o["symbol"] == sym and o["side"] == side and o["status"] in ACTIVE
                 and o["order_id"] not in known]
        if other:
            raise Halted(f"二重発注の疑い: {sym} の{'買い' if side == 'BUY' else '売り'}注文がボットの記録にない形で "
                         f"有効です (注文 {', '.join(o['order_id'] for o in other)})。手動の注文なら取り消してから解除してください")
        self._count_order()
        try:
            return place()
        except BrokerError:
            # 通信が切れただけで注文は通っていることがある → 目印で探し直す
            try:
                again = [o for o in self.broker.orders() if o["remark"] == rmk and o["status"] not in DEAD]
            except BrokerError:
                again = []
            if again:
                log.warning("発注の応答はエラーでしたが、注文 %s は出ていました", again[0]["order_id"])
                return again[0]
            raise

    # ---------------------------------------------------------------- 照合
    def reconcile(self) -> None:
        """ボットの記録 (建玉・注文) を証券会社の記録に合わせる。合わないものがあれば、全部見終わってから停止する。"""
        today = self._session_today()
        now = self.now()
        led = self._ledger()
        problems: list[str] = []
        keep = []
        for o in self.st["orders"]:
            if not o.get("order_id"):
                over = now >= calendar_us.session_times_jst(date.fromisoformat(o["for_date"]))[1]
                if over:                # 出せないまま執行日が終わった → 捨てる (売りは逆指値が残っているので次の判断で)
                    log.warning("[期限切れ] %s の%s注文は %s に出せませんでした (取り消し扱い)", o["symbol"],
                                "買い" if o["kind"] == "entry" else "売り", o["for_date"])
                else:
                    keep.append(o)      # まだ出していない (予約できなかった) 注文
                continue
            b = self.broker.order(o["order_id"])
            if b is None or b["status"] in UNKNOWN:
                keep.append(o)          # 分からないものは消さずに残す
                problems.append(f"注文 {o['order_id']} ({o['symbol']}) の結果が分かりません "
                                f"({'証券会社の記録に見つからない' if b is None else '状態 ' + b['status']})")
                continue
            for_date = date.fromisoformat(o["for_date"])
            session_over = now >= calendar_us.session_times_jst(for_date)[1]
            if b["status"] in ACTIVE and not session_over:
                keep.append(o)
                continue
            if b["status"] in ACTIVE:       # 取引時間が終わっても有効のまま → 未約定。取り消す
                self.broker.cancel(o["order_id"])
            dealt = b["dealt_qty"]
            side = "買い" if o["kind"] == "entry" else "売り"
            if dealt > 0:
                if o["kind"] == "entry":
                    self._open_position(o, dealt, b["dealt_avg_price"], for_date, led)
                else:
                    self._close_position(o["symbol"], dealt, b["dealt_avg_price"], o["reason"], for_date, led)
            if dealt + 1e-9 < o["qty"] and o.get("limit_price") and b["status"] not in UNKNOWN:
                log.info("[見送り] %s: 寄り付きが上限 %.2f を超えたので買いませんでした (約定 %g 株)", o["symbol"],
                         o["limit_price"], dealt)
            elif dealt + 1e-9 < o["qty"]:
                problems.append(f"{o['symbol']} の{side}注文が{'一部しか' if dealt else ''}約定しませんでした "
                                f"(約定 {dealt:g} / {o['qty']:g} 株・状態 {b['status']}"
                                f"{'・' + b['err'] if b['err'] else ''})")
        self.st["orders"] = keep

        # 逆指値の約定
        for sym in list(self.st["positions"]):
            p = self.st["positions"][sym]
            sid = p.get("stop_order_id")
            if not sid:
                continue
            b = self.broker.order(sid)
            why = "trailing_stop" if p.get("trailing") else "stop"
            if b is not None and b["status"] in UNKNOWN:
                problems.append(f"{sym} の逆指値 (注文 {sid}) の状態が分かりません ({b['status']})")
            elif b is not None and b["status"] in FILLED:
                self._close_position(sym, b["dealt_qty"], b["dealt_avg_price"], why, today, led)
            elif b is None or b["status"] in DEAD | PARTIAL_DONE:
                if b is not None and b["dealt_qty"] > 0:
                    self._close_position(sym, b["dealt_qty"], b["dealt_avg_price"], why, today, led)
                if sym in self.st["positions"]:
                    log.warning("%s の逆指値 (注文 %s) が有効ではありません (%s) → 入れ直します",
                                sym, sid, b["status"] if b else "見つからない")
                    self.st["positions"][sym]["stop_order_id"] = None
        self._save_ledger(led)
        self.save()

        # 保有株数 (証券会社の方が少なければ、ボットの知らないところで売られている)
        held = self.broker.positions()
        for sym, p in self.st["positions"].items():
            have = (held.get(sym) or {}).get("qty", 0.0)
            if have + 1e-9 < p["shares"]:
                problems.append(f"{sym} の保有株数が合いません (ボットの記録 {p['shares']:g} 株・証券会社 {have:g} 株)")
        if problems:
            raise Halted(" / ".join(problems))

    def _open_position(self, o: dict, qty: float, price: float | None, d: date, led) -> None:
        if not price or price <= 0:
            raise Halted(f"{o['symbol']} の約定価格が分かりません")
        cost = risk.buy_total(qty, price, self.cfg)
        led.settled -= cost                      # 実際の約定で帳簿を付ける (手数料は推定)
        stop = strategy.initial_stop(price, o["atr"], self.cfg)
        pos = Position(o["symbol"], qty, d, 0, price, o["atr"], stop, price, entry_cost=cost, rank=o.get("rank", 0))
        self.st["positions"][o["symbol"]] = pos_to_dict(pos, stop_order_id=None, next_earnings=o.get("next_earnings"))
        log.info("[約定] 買い %s %s株 @%.2f → 損切り %.2f", o["symbol"], qty, price, stop)
        self.save()

    def _close_position(self, sym: str, qty: float, price: float | None, reason: str, d: date, led) -> None:
        p = self.st["positions"].get(sym)
        if p is None:
            return
        price = price or 0.0
        net = risk.sell_net(qty, price, self.cfg)
        avail = calendar_us.add_trading_days(d, self.cfg.account["settlement_days"])
        led.add_sale(avail, net)
        part = qty / p["shares"] if p["shares"] else 1.0
        cost = p["entry_cost"] * part
        self.st["trades"].append({"symbol": sym, "entry_date": p["entry_date"], "exit_date": d.isoformat(),
                                  "shares": qty, "entry_price": p["entry_price"], "exit_price": price,
                                  "pnl": round(net - cost, 2), "reason": reason})
        log.info("[約定] 売り %s %s株 @%.2f (%s) 損益 %+.2f ドル (受渡 %s)", sym, qty, price, reason, net - cost, avail)
        if qty + 1e-9 >= p["shares"]:
            del self.st["positions"][sym]
            self.st["cooldown"][sym] = calendar_us.add_trading_days(d, self.cfg.strategy["reentry_cooldown_days"]).isoformat()
        else:
            p["shares"] -= qty
            p["entry_cost"] -= cost
            p["stop_order_id"] = None            # 残りの株数で逆指値を入れ直す
        self.save()

    # ---------------------------------------------------------------- 逆指値
    def ensure_stops(self) -> None:
        """すべての建玉に、証券会社側の逆指値が入っている状態にする。"""
        selling = {o["symbol"] for o in self.st["orders"] if o["kind"] == "exit" and o.get("order_id")}
        for sym, p in list(self.st["positions"].items()):
            if p.get("stop_order_id") or sym in selling:      # 成行の売りが出ている建玉には入れない (売りが 2 重になる)
                continue
            d = date.fromisoformat(p["entry_date"])
            try:
                o = self._submit(remark(d, sym, f"S{int(p['shares'])}"), sym, "SELL",
                                 lambda: self.broker.stop(sym, p["shares"], p["stop"], remark(d, sym, f"S{int(p['shares'])}")))
            except BrokerError as e:
                self._stop_failed(sym, p, str(e))
                continue
            p["stop_order_id"] = o["order_id"]
            self.save()

    def _stop_failed(self, sym: str, p: dict, why: str) -> None:
        log.error("%s に逆指値を入れられません: %s", sym, why)
        if self.ex["on_stop_failure"] == "flatten":
            today = self._session_today()
            try:
                rmk = remark(today, sym, "X")
                o = self._submit(rmk, sym, "SELL", lambda: self.broker.market(sym, "SELL", p["shares"], rmk))
                self.st["orders"].append({"kind": "exit", "symbol": sym, "qty": p["shares"], "reason": "no_stop",
                                          "for_date": calendar_us.next_trading_day(today).isoformat()
                                          if self.now() >= calendar_us.session_times_jst(today)[1] else today.isoformat(),
                                          "remark": o["remark"], "order_id": o["order_id"]})
                log.error("%s を成行で売る注文を出しました (逆指値なしで持たないため)", sym)
            except (BrokerError, Halted) as e:
                log.error("%s の成行売りも出せませんでした: %s", sym, e)
        raise Halted(f"{sym} に証券会社側の逆指値を入れられません ({why})")

    # ---------------------------------------------------------------- 日足
    def _bars(self, sym: str, d: date) -> pd.DataFrame | None:
        from . import data
        old = data.read_prices(self.cfg, sym, "live")
        md = self.cfg["moomoo_data"]
        if old is not None and len(old) >= self.cfg.screener["sma_slow"] + 20:
            start = (old.index.max() - pd.Timedelta(days=10)).date()
        else:
            start = d - timedelta(days=md["bars_calendar_days"])
        try:
            new = self.md.daily_bars(sym, start.isoformat(), d.isoformat())
        except Exception as e:  # noqa: BLE001 - 1 銘柄の失敗で止めない (その銘柄は判断しない)
            log.warning("日足 %s を取得できません: %s", sym, e)
            return old
        df = data._merge(old, new) if new is not None and len(new) else old
        if df is not None and len(df):
            data.write_prices(self.cfg, sym, df, kind="live")
        return df

    def market_panel(self, d: date, symbols: list[str]) -> tuple[dict, int] | None:
        """symbols とベンチマークの日足で指標を計算する。最新の日足が d でなければ None (判断しない)。"""
        from .indicators import compute_all
        bench = self._bars(self.cfg.data.benchmark, d)
        if bench is None or not len(bench) or bench.index[-1].date() != d:
            log.warning("ベンチマーク (%s) の %s の日足がまだありません → 今日の判断は見送ります", self.cfg.data.benchmark, d)
            return None
        idx = bench.index
        bars = {s: self._bars(s, d) for s in symbols}
        bars = {s: b for s, b in bars.items() if b is not None and len(b)}
        panel = {k: pd.DataFrame({s: b[k].reindex(idx) for s, b in bars.items()}, index=idx, dtype="float64")
                 for k in ("open", "high", "low", "close", "volume")}
        panel["bench_close"] = bench["close"]
        return compute_all(panel, self.cfg), len(idx) - 1

    def watchlist(self, d: date) -> dict | None:
        from .screener import load_watchlist
        wdir = self.cfg.path("watchlists")
        files = sorted(wdir.glob("watchlist_*.json")) if wdir.exists() else []
        for f in reversed(files):
            wl = load_watchlist(f)
            dd = date.fromisoformat(wl["data_date"])
            if dd <= d:
                if (d - dd).days > self.ex["watchlist_max_age_days"]:
                    log.warning("監視リスト %s が古いので新規エントリーはしません (データ %s)", f.name, dd)
                    return None
                return wl
        log.warning("監視リストがありません → 新規エントリーはしません")
        return None

    def _earnings(self, d: date) -> dict[str, list[date]] | None:
        try:
            return self.md.earnings_calendar(d + timedelta(days=1), d + timedelta(days=self.ex["earnings_refresh_days"]))
        except Exception as e:  # noqa: BLE001
            log.warning("決算カレンダーを取得できません (監視リスト・前回の値を使います): %s", e)
            return None

    # ---------------------------------------------------------------- 引け後の処理
    def run_close(self, force: bool = False) -> dict:
        d = calendar_us.last_completed_session(self.now())
        try:
            self._init_capital()
            if self.st["last_close"] == d.isoformat() and not force:
                log.info("%s の引け後の処理は済んでいます", d)
                return self.summary(d)
            self.reconcile()
            if self.halted:
                log.warning("停止中のため、判断と新しい注文はしません (理由: %s)", self.st["halt"]["reason"])
                self.ensure_stops()
                return self.summary(d)
            self.ensure_stops()
            if not self._decide(d):
                return self.summary(d)          # 日足がまだ → もう一度実行すればやり直す
            if self.ex["order_timing"] == "reserve":
                self.place_pending(calendar_us.next_trading_day(d), reserve=True)
            self.st["last_close"] = d.isoformat()
            self.save()
        except Halted as e:
            self.halt(str(e))
            self._try(self.ensure_stops)
        except BrokerError as e:
            self.halt(f"OpenD との通信エラー: {e}")
        return self.summary(d)

    def _try(self, fn) -> None:
        try:
            fn()
        except (Halted, BrokerError) as e:
            log.error("%s", e)

    def _decide(self, d: date) -> bool:
        cfg, sc = self.cfg, self.cfg.strategy
        nxt = calendar_us.next_trading_day(d)
        wl = self.watchlist(d)
        items = (wl or {}).get("items") or []
        held = list(self.st["positions"])
        got = self.market_panel(d, sorted(set(held) | {x["symbol"] for x in items}))
        if got is None:
            return False
        ind, i = got
        v = lambda k, s: _val(ind, k, i, s)        # noqa: E731
        led = self._ledger()
        led.settle(d)

        # 時価評価・週次の損失上限
        mv = sum(p["shares"] * (v("close", s) or p["entry_price"]) for s, p in self.st["positions"].items())
        equity = led.total + mv
        f = self.broker.funds()
        wk = "%d-%02d" % d.isocalendar()[:2]
        if self.st["week"].get("key") != wk:
            self.st["week"] = {"key": wk, "start_equity": self.st["last_equity"] or equity, "tripped": False}
        w = self.st["week"]
        if (equity - w["start_equity"]) / w["start_equity"] <= -cfg.risk["weekly_loss_limit_pct"] / 100.0:
            if not w["tripped"]:
                log.warning("今週の損失が資金の %s%% に達しました → 今週は新規エントリーを止めます", cfg.risk["weekly_loss_limit_pct"])
            w["tripped"] = True
        self.st["last_equity"] = equity
        hist = [x for x in self.st.setdefault("equity_history", []) if x[0] != d.isoformat()]
        self.st["equity_history"] = (hist + [[d.isoformat(), round(equity, 2)]])[-400:]    # 画面の資金の推移用
        log.info("[%s] ボットの資金 %.2f ドル (現金 %.2f・株 %.2f)・建玉 %d", d, equity, led.total, mv, len(self.st["positions"]))

        b, bs = ind["bench_close"].iloc[i], ind["bench_sma"].iloc[i]
        index_ok = bool(pd.notna(b) and pd.notna(bs) and b >= bs)
        cal = self._earnings(d)

        # 損切りの更新と手仕舞い判定
        exits = []
        for sym, p in self.st["positions"].items():
            cl = v("close", sym)
            if cl is None:
                log.warning("%s の %s の終値がありません → この銘柄の判断は見送ります", sym, d)
                continue
            pos = pos_from_dict(p)
            old = pos.stop
            strategy.update_stop(pos, cl, v("high", sym), v("sma_trail", sym), v("atr", sym), cfg)
            if pos.stop > old + 0.005 and p.get("stop_order_id"):
                self.broker.modify_stop(p["stop_order_id"], p["shares"], pos.stop)
                log.info("[損切り更新] %s %.2f → %.2f", sym, old, pos.stop)
            p.update(stop=pos.stop, highest=pos.highest, trailing=pos.trailing)
            if cal is not None:
                fut = [x for x in cal.get(sym, []) if x > d]
                if fut:
                    p["next_earnings"] = fut[0].isoformat()
            ne = date.fromisoformat(p["next_earnings"]) if p.get("next_earnings") else None
            edays = calendar_us.trading_days_between(d, ne) if ne and ne > d else None
            held_days = calendar_us.trading_days_between(pos.entry_date, d)
            reason = strategy.exit_reason(pos, held_days, cl, edays, index_ok, cfg)
            if reason:
                exits.append(sym)
                est = risk.sell_net(p["shares"], cl * (1 - cfg.fees["slippage_pct"] / 100.0), cfg)
                self.st["orders"].append({"kind": "exit", "symbol": sym, "qty": p["shares"], "reason": reason,
                                          "for_date": nxt.isoformat(), "remark": remark(nxt, sym, "X"), "order_id": None,
                                          # 画面表示用の目安 (成行なので実際の値段は翌寄り付きで決まる)
                                          "ref_price": round(cl, 4), "est_amount": round(est, 2),
                                          "est_pnl": round(est - p["entry_cost"], 2)})
                log.info("[手仕舞い判定] %s → 翌寄り付き (%s) で売り (%s)", sym, nxt, reason)
        self.save()

        # 新規エントリー判定
        if sc["index_filter"] and not index_ok:
            log.info("[新規停止] 指数フィルター (%s 終値 < %s 日線)", cfg.data.benchmark, cfg.screener["index_sma"])
            return True
        if w["tripped"]:
            log.info("[新規停止] 今週の損失上限")
            return True
        if not wl or not wl.get("index_ok", True):
            return True
        free = sc["max_positions"] - (len(self.st["positions"]) - len(exits))
        free -= sum(1 for o in self.st["orders"] if o["kind"] == "entry")
        size_equity = equity
        if cfg.account["capital_source"] == "account" and f.get("total_assets") is not None:
            size_equity = min(equity, f["total_assets"])
        cash = led.available(nxt)
        if f.get("power") is not None:
            if f["power"] < cash:
                log.info("証券会社の買付余力 (%.2f ドル) がボットの現金 (%.2f ドル) より少ないので、買付余力の範囲で買います"
                         "%s", f["power"], cash, "（ドルの買付余力が 0 なら、円からドルへの両替が必要かもしれません）" if f["power"] <= 0 else "")
            cash = min(cash, f["power"])
        slip = cfg.fees["slippage_pct"] / 100.0
        for it in items:
            if free <= 0:
                break
            sym = it["symbol"]
            if sym in self.st["positions"] or any(o["symbol"] == sym for o in self.st["orders"]):
                continue
            cd = self.st["cooldown"].get(sym)
            if cd and nxt <= date.fromisoformat(cd):
                continue
            if not strategy.entry_signal(v("close", sym), v("high_prev", sym), v("volume", sym), v("avg_vol_prev", sym), cfg):
                continue
            a, cl = v("atr", sym), v("close", sym)
            n = risk.position_size(size_equity, cl, a, cfg, cash)
            cap_jpy = self.ex.get("max_order_jpy")
            if cap_jpy and n > 0:                  # 1 注文の金額の上限 (本番ベータ)
                from . import fx as fx_mod
                rate, _ = fx_mod.current(cfg)
                n_cap = int((cap_jpy / rate) // risk.buy_total(1, cl * (1 + slip), cfg))
                if n_cap < n:
                    log.info("[上限] %s: 1 注文の上限 %s 円に合わせて %d 株 → %d 株", sym, f"{cap_jpy:,}", n, n_cap)
                    n = n_cap
            if n <= 0:
                log.info("[見送り] %s: 株数が 1 株未満か、受渡済みの現金・買付余力が足りません", sym)
                continue
            ne = it.get("next_earnings")
            if cal is not None and cal.get(sym):
                fut = [x for x in cal[sym] if x > d]
                ne = fut[0].isoformat() if fut else ne
            est = risk.buy_total(n, cl * (1 + slip), cfg)
            cash -= est
            stop_est = strategy.initial_stop(cl * (1 + slip), a, cfg)
            self.st["orders"].append({"kind": "entry", "symbol": sym, "qty": n, "atr": a, "rank": it["rank"],
                                      "next_earnings": ne, "for_date": nxt.isoformat(),
                                      "remark": remark(nxt, sym, "B"), "order_id": None,
                                      # 画面表示用の目安 (成行なので実際の値段は翌寄り付きで決まる)
                                      "name": it.get("name"), "ref_price": round(cl, 4), "est_amount": round(est, 2),
                                      "est_stop": round(stop_est, 2),
                                      "est_risk": round(n * (cl * (1 + slip) - stop_est), 2)})
            g = sc["entry_limit_gap_pct"]
            if g is not None:      # 上限付きの指値 (前日終値 × (1 + g%) より高く始まったら約定しない → 見送り)
                self.st["orders"][-1]["limit_price"] = round(cl * (1 + g / 100.0), 2)
            log.info("[エントリー判定] %s (順位 %d) 終値 %.2f > 20日高値 %.2f・出来高 %.0f → 翌寄り付き (%s) に %d 株",
                     sym, it["rank"], cl, v("high_prev", sym), v("volume", sym), nxt, n)
            free -= 1
        self.save()
        return True

    # ---------------------------------------------------------------- 発注
    def place_pending(self, for_date: date, reserve: bool = False) -> None:
        """まだ出していない注文を出す (売り → 買いの順)。予約を受け付けられなかった注文は寄り付き後に出す。"""
        todo = [o for o in self.st["orders"] if not o.get("order_id") and o["for_date"] == for_date.isoformat()]
        todo.sort(key=lambda o: 0 if o["kind"] == "exit" else 1)
        for o in todo:
            if self.halted:
                return
            try:
                if o["kind"] == "exit":
                    self._place_exit(o)
                else:
                    if o.get("limit_price"):
                        r = self._submit(o["remark"], o["symbol"], "BUY",
                                         lambda: self.broker.limit(o["symbol"], "BUY", o["qty"], o["limit_price"], o["remark"]))
                    else:
                        r = self._submit(o["remark"], o["symbol"], "BUY",
                                         lambda: self.broker.market(o["symbol"], "BUY", o["qty"], o["remark"]))
                    o["order_id"] = r["order_id"]
            except BrokerError as e:
                if reserve:
                    log.warning("%s の注文を予約できませんでした (寄り付き後の処理で出します): %s", o["symbol"], e)
                    continue
                raise
            self.save()

    def _place_exit(self, o: dict) -> None:
        """手仕舞い: 逆指値を取り消してから成行で売る (同時に残すと売りが 2 重になるため)。"""
        p = self.st["positions"].get(o["symbol"])
        if p is None:
            self.st["orders"].remove(o)
            return
        sid = p.get("stop_order_id")
        if sid:
            self.broker.cancel(sid)
            b = None
            for _ in range(10):                 # 取消が終わったのを確かめてから売る
                b = self.broker.order(sid)
                if b is None or b["status"] in DEAD | FILLED | PARTIAL_DONE:
                    break
                self.sleep(1)
            if b is not None and (b["dealt_qty"] > 0 or b["status"] not in DEAD):
                raise Halted(f"{o['symbol']} の逆指値を取り消せたか確認できません (状態 {b['status']}・"
                             f"約定 {b['dealt_qty']:g} 株)。次の照合で記録します")
            p["stop_order_id"] = None
            self.save()
        try:
            r = self._submit(o["remark"], o["symbol"], "SELL",
                             lambda: self.broker.market(o["symbol"], "SELL", p["shares"], o["remark"]))
        except BrokerError:
            self._try(self.ensure_stops)        # 売れないなら逆指値を戻す
            raise
        o["order_id"], o["qty"] = r["order_id"], p["shares"]

    # ---------------------------------------------------------------- 寄り付き後の処理
    def run_open(self) -> dict:
        now = self.now()
        d = now.astimezone(calendar_us.NY).date()
        o_jst, c_jst = calendar_us.session_times_jst(d)
        if not calendar_us.is_trading_day(d) or not (o_jst <= now < c_jst):
            log.warning("今は米国の取引時間外です (寄り付き %s)。寄り付き後に実行してください", o_jst.strftime("%m/%d %H:%M"))
            return self.summary(d)
        try:
            self._init_capital()
            self.reconcile()
            if not self.halted:
                self.place_pending(d)
            self._wait_fills(d)
            self.ensure_stops()
            self.st["last_open"] = d.isoformat()
            self.save()
        except Halted as e:
            self.halt(str(e))
            self._try(self.ensure_stops)
        except BrokerError as e:
            self.halt(f"OpenD との通信エラー: {e}")
            self._try(self.ensure_stops)
        return self.summary(d)

    def _wait_fills(self, d: date) -> None:
        """今日の注文が約定するまで待つ (最大 fill_wait_sec)。約定した買いにはすぐ逆指値を入れる。"""
        polls = max(int(self.ex["fill_wait_sec"] // max(self.ex["poll_sec"], 1)), 0)
        for k in range(polls + 1):
            self.reconcile()
            self.ensure_stops()
            waiting = [o for o in self.st["orders"] if o.get("order_id") and o["for_date"] == d.isoformat()]
            if not waiting:
                return
            if k < polls:
                self.sleep(self.ex["poll_sec"])
        led = self._ledger()
        for o in waiting:            # 時間内に約定しなかった → 取り消す (成行なら停止。上限付きの指値は見送り)
            self.broker.cancel(o["order_id"])
            b = self.broker.order(o["order_id"]) or {"dealt_qty": 0, "dealt_avg_price": None}
            self.st["orders"].remove(o)
            if b["dealt_qty"] > 0:
                if o["kind"] == "entry":
                    self._open_position(o, b["dealt_qty"], b["dealt_avg_price"], d, led)
                else:
                    self._close_position(o["symbol"], b["dealt_qty"], b["dealt_avg_price"], o["reason"], d, led)
            if o.get("limit_price") and o["kind"] == "entry":
                log.info("[見送り] %s: 寄り付きが上限 %.2f を超えたので買いませんでした (約定 %g 株・取消)",
                         o["symbol"], o["limit_price"], b["dealt_qty"])
        self._save_ledger(led)
        self.save()
        self._try(self.ensure_stops)
        bad = [o for o in waiting if not (o.get("limit_price") and o["kind"] == "entry")]
        if bad:
            raise Halted("寄り付き後 %d 秒たっても約定しない注文がありました (%s)。取り消しました"
                         % (self.ex["fill_wait_sec"], ", ".join(o["symbol"] for o in bad)))

    # ---------------------------------------------------------------- まとめ
    def summary(self, d: date) -> dict:
        s = {"env": self.env, "date": d.isoformat(), "time": self.now().isoformat(timespec="seconds"),
             "halt": self.st["halt"], "capital_usd": self.st["capital_usd"],
             "cash_settled_usd": self.st["cash_settled"], "pending_sales": self.st["pending_sales"],
             "equity_usd": self.st["last_equity"], "week": self.st["week"],
             "positions": self.st["positions"], "orders": self.st["orders"],
             "trades_recent": self.st["trades"][-20:], "last_close": self.st["last_close"],
             "last_open": self.st["last_open"]}
        self.st["last_summary"] = s["time"]
        self.save()
        out = self.cfg.path(self.ex["state_dir"], f"summary_{self.env}_{d:%Y%m%d}.json")
        out.write_text(json.dumps(s, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        out.with_suffix(".md").write_text(summary_text(s), encoding="utf-8")
        return s


def summary_text(s: dict) -> str:
    """毎日のまとめ (人が読む用): 建玉と損益、翌日の予約注文、停止の状態。"""
    env = "模擬口座" if s["env"] == "SIMULATE" else "本番口座"
    L = [f"# スイング bot まとめ {s['date']}（{env}・{s['time']}）", ""]
    h = s["halt"]
    L.append(f"**⛔ 発注停止中**: {h['reason']}（{h['time']}）" if h.get("on") else "発注: 通常どおり")
    eq, cap = s.get("equity_usd"), s.get("capital_usd")
    if cap:
        L.append(f"ボットの資金: ${eq or cap:,.2f}（開始 ${cap:,.2f}・{(((eq or cap) / cap) - 1) * 100:+.2f}%）"
                 f"・受渡済み現金 ${s['cash_settled_usd']:,.2f}")
    if s["week"].get("tripped"):
        L.append("今週の損失上限に達したので、今週は新規エントリーなし")
    L += ["", "## 建玉", ""]
    if s["positions"]:
        L.append("| 銘柄 | 株数 | 買値 | 損切り | 逆指値の注文 | 建玉日 | 次回決算 |")
        L.append("|---|---:|---:|---:|---|---|---|")
        for p in s["positions"].values():
            L.append(f"| {p['symbol']} | {p['shares']:g} | {p['entry_price']:.2f} | {p['stop']:.2f}"
                     f"{' (トレーリング)' if p.get('trailing') else ''} | {p.get('stop_order_id') or '❌ なし'} | "
                     f"{p['entry_date']} | {p.get('next_earnings') or '—'} |")
    else:
        L.append("なし")
    L += ["", "## 予約・発注中の注文", ""]
    if s["orders"]:
        for o in s["orders"]:
            L.append(f"- {o['for_date']} {o['symbol']} {'買い' if o['kind'] == 'entry' else '売り'} {o['qty']:g} 株"
                     f"（{o.get('reason') or '20日高値ブレイク'}・{'発注済み ' + o['order_id'] if o.get('order_id') else '未発注'}）")
    else:
        L.append("なし")
    L += ["", "## 最近の取引", ""]
    tr = s.get("trades_recent") or []
    if tr:
        for t in tr[-10:]:
            L.append(f"- {t['symbol']} {t['entry_date']} {t['entry_price']:.2f} → {t['exit_date']} {t['exit_price']:.2f}"
                     f" × {t['shares']:g} 株: {t['pnl']:+.2f} ドル（{t['reason']}）")
        L.append(f"- 合計（直近 {len(tr)} 件）: {sum(t['pnl'] for t in tr):+.2f} ドル")
    else:
        L.append("まだありません")
    return "\n".join(L) + "\n"


def _val(ind: dict, k: str, i: int, s: str):
    df = ind[k]
    if s not in df.columns:
        return None
    x = df.iat[i, df.columns.get_loc(s)]
    return None if pd.isna(x) else float(x)


# ---------------------------------------------------------------- 発注機能の確認 (模擬口座だけ)
def capability_check(cfg, broker, regular_hours: bool | None = None, sleep=time.sleep, now=None) -> dict:
    """模擬口座で、ボットが使う注文が受け付けられるかを確かめる。本番口座では動かない。

    いつでも: 指値の買い (届かない値段) → 訂正 → 取消 / 成行の予約 (時間外に出して受け付けるか → 取消)
    取引時間中だけ: 成行で 1 株買う → 逆指値 (GTC) → 訂正 → 取消 → 成行で売る
    """
    if broker.env_name != "SIMULATE":
        raise RuntimeError("発注機能の確認は模擬口座 (SIMULATE) でしか動かしません")
    sym = cfg["executor"]["test_symbol"]
    now = now or datetime.now(calendar_us.TOKYO)
    d = now.astimezone(calendar_us.NY).date()
    o_jst, c_jst = calendar_us.session_times_jst(d)
    if regular_hours is None:
        regular_hours = calendar_us.is_trading_day(d) and o_jst <= now < c_jst
    res: dict = {"time": now.isoformat(timespec="seconds"), "symbol": sym, "regular_hours": regular_hours, "steps": []}

    def step(name, fn):
        try:
            out = fn()
            res["steps"].append({"name": name, "ok": True, "detail": out})
            log.info("[確認] ✔ %s %s", name, out or "")
            return out
        except Exception as e:  # noqa: BLE001 - 結果を表にする
            res["steps"].append({"name": name, "ok": False, "detail": str(e)})
            log.warning("[確認] ✖ %s: %s", name, e)
            return None

    def wait_status(oid, want, secs=20):
        st = None
        for _ in range(secs):
            o = broker.order(oid)
            st = o["status"] if o else None
            if st in want:
                return o
            sleep(1)
        raise RuntimeError(f"状態が {want} になりません (今: {st})")

    step("口座の資産を読む", lambda: {k: broker.funds().get(k) is not None for k in ("total_assets", "cash", "power")})
    tag = f"swb:test:{now:%H%M%S}"
    last = cfg_last_close(cfg, sym)
    lim = round(max((last or 100.0) * 0.5, 1.0), 2)
    o = step("指値の買い (約定しない値段)", lambda: broker.limit(sym, "BUY", 1, lim, tag + ":L"))
    if o:
        step("指値の訂正", lambda: broker.modify_limit(o["order_id"], 1, round(lim * 0.99, 2)))
        step("注文の照会 (目印が残るか)", lambda: {"remark_ok": (broker.order(o["order_id"]) or {}).get("remark") == tag + ":L"})
        step("取消", lambda: (broker.cancel(o["order_id"]), wait_status(o["order_id"], DEAD)["status"])[1])
    if not regular_hours:
        m = step("時間外に成行を予約 (翌寄り付き用)", lambda: broker.market(sym, "BUY", 1, tag + ":M"))
        if m:
            b = broker.order(m["order_id"]) or {}
            res["reserve_status"] = b.get("status")
            step("予約した成行の取消", lambda: (broker.cancel(m["order_id"]), wait_status(m["order_id"], DEAD | FILLED)["status"])[1])
        res["order_timing_hint"] = "reserve" if m and res.get("reserve_status") not in DEAD else "at_open"
    else:
        b = step("成行で 1 株買う", lambda: wait_status(broker.market(sym, "BUY", 1, tag + ":B")["order_id"], FILLED, 60))
        if b:
            px = b["dealt_avg_price"] or last or 100.0
            s = step("逆指値の売り (GTC)", lambda: broker.stop(sym, 1, px * 0.8, tag + ":S"))
            if s:
                step("逆指値の訂正", lambda: broker.modify_stop(s["order_id"], 1, px * 0.81))
                step("逆指値の照会", lambda: {k: (broker.order(s["order_id"]) or {}).get(k) for k in ("status", "type", "aux_price")})
                step("逆指値の取消", lambda: (broker.cancel(s["order_id"]), wait_status(s["order_id"], DEAD)["status"])[1])
            step("成行で売って元に戻す", lambda: wait_status(broker.market(sym, "SELL", 1, tag + ":X")["order_id"], FILLED, 60)["status"])
    res["ok"] = all(x["ok"] for x in res["steps"])
    p = cfg.path(cfg["executor"]["state_dir"], "capability_SIMULATE.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return res


def cfg_last_close(cfg, sym: str) -> float | None:
    from . import data
    for kind in ("live", "history"):
        df = data.read_prices(cfg, sym, kind)
        if df is not None and len(df):
            return float(df["close"].iloc[-1])
    return None


# ---------------------------------------------------------------- 組み立て
class Session:
    """with Session(cfg) as ex: ... で、口座の確認 → 接続 → 実行 → 切断 まで行う。

    confirm: 本番口座 (REAL) のときに呼ぶ確認 (コマンドの入力)。画面からは渡さないので REAL は開けない。
    """

    def __init__(self, cfg, confirm=None, password_store=None):
        self.cfg, self.confirm, self.pws = cfg, confirm, password_store
        self.broker = self.md = None
        self.lock = RunLock(cfg.path(cfg["executor"]["state_dir"], "run.lock"))

    def __enter__(self) -> Executor:
        from .broker import Broker, resolve_env
        from .moomoo_data import MoomooData
        env, unlock = resolve_env(self.cfg, self.confirm)
        pw = None
        if env == "REAL":
            from .passwords import PasswordStore
            pw, _ = (self.pws or PasswordStore()).get()
        self.lock.__enter__()
        from . import fx
        fx.refresh(self.cfg)                       # 今の為替 (取れなければ前の値・設定値)
        try:
            self.broker = Broker(self.cfg, env, unlock, pw)
            self.md = MoomooData(self.cfg)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return Executor(self.cfg, self.broker, env, self.md)

    def __exit__(self, *exc):
        for x in (self.md, self.broker):
            if x is not None:
                try:
                    x.close()
                except Exception:  # noqa: BLE001
                    pass
        self.lock.__exit__(*exc)


def read_summary(cfg, env: str = "SIMULATE") -> dict:
    """画面表示用: 保存されている状態 (接続しない)。"""
    st = load_state(cfg, env)
    cap = cfg.path(cfg["executor"]["state_dir"], f"capability_{env}.json")
    return {"env": env, "halt": st["halt"], "capital_usd": st["capital_usd"], "cash_settled_usd": st["cash_settled"],
            "pending_sales": st["pending_sales"], "equity_usd": st["last_equity"], "week": st["week"],
            "positions": st["positions"], "orders": st["orders"], "trades": st["trades"][-30:],
            "last_close": st["last_close"], "last_open": st["last_open"], "created": st["created"],
            "equity_history": st.get("equity_history", []),
            "capability": json.loads(cap.read_text(encoding="utf-8")) if cap.exists() else None}
