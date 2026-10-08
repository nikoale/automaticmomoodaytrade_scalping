"""moomoo OpenD からリアルタイム相場を受け取って売買するランナー。

スレッド構成:
  moomoo のプッシュ (K線 / 現在値 / 板) は SDK の別スレッドで届く → queue に積むだけ
  メインスレッドが queue を取り出してエンジンを動かす (エンジンはシングルスレッド前提)
"""
from __future__ import annotations

import csv
import logging
import queue
import signal
import threading
import time as _time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .broker import Broker, SimBroker
from .config import Config
from .data import parse_time
from .engine import SymbolEngine
from .market import TradingSessions
from .models import Bar, Side, Trade
from .risk import RiskManager
from .strategies import create_strategy

log = logging.getLogger(__name__)

_KTYPE = {1: "K_1M", 3: "K_3M", 5: "K_5M", 15: "K_15M"}


class PaperBroker(SimBroker):
    """paper モード: moomoo の実際の板 (最良気配) で約定したと仮定するローカル模擬約定。"""

    def __init__(self, cfg: Config, best_quote):
        super().__init__(cfg.execution, cfg.market)
        self.best_quote = best_quote

    def execute(self, code, side, qty, ref_price, t, reason="", exact=False):
        bid, ask = self.best_quote(code)
        book = ask if side == Side.BUY else bid
        fill = super().execute(code, side, qty, book or ref_price, t, reason, exact=False)
        if fill:
            log.info("[PAPER] %s %s %g @ %.4f (%s)", code, side.value, qty, fill.price, reason)
        return fill


class TradeLogger:
    def __init__(self, log_dir: str, mode: str):
        self.path = Path(log_dir) / f"trades_{mode}_{datetime.now():%Y%m%d}.csv"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with open(self.path, "w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(["code", "direction", "qty", "entry_time", "entry_price", "exit_time",
                                         "exit_price", "pnl", "reason", "bars_held"])

    def __call__(self, t: Trade) -> None:
        with open(self.path, "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow([t.code, t.direction, t.qty, t.entry_time, t.entry_price, t.exit_time,
                                     t.exit_price, round(t.pnl, 2), t.reason, t.bars_held])


class LiveRunner:
    def __init__(self, cfg: Config):
        if cfg.mode not in ("paper", "simulate", "live"):
            raise ValueError("LiveRunner は paper / simulate / live モード用です")
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.session.timezone)
        self.events: queue.Queue = queue.Queue()
        self.book: dict[str, tuple[float | None, float | None]] = {}
        self._pending_bar: dict[str, dict] = {}   # 形成中の足 (最後に届いたプッシュ)
        self._last_bar_time: dict[str, datetime] = {}
        self._stop = False
        self.engines: dict[str, SymbolEngine] = {}
        self.quote_ctx = None
        self.broker: Broker | None = None
        self.bars: dict[str, deque] = {}          # 画面表示用の直近の確定足
        self.phase = "idle"             # idle / connecting / screening / warming / running / stopped / error
        self.error: str | None = None
        self.started_at: datetime | None = None
        self.paused = False
        self._blocked: set[str] = set()      # 既存ポジションがあるので触らない銘柄
        self._retiring: set[str] = set()     # 自動選定から外れた (決済待ち) 銘柄
        self._next_refresh: float | None = None
        self._last_session_idx: int | None = None
        self.next_refresh_at: datetime | None = None
        self.last_refresh_at: datetime | None = None
        self.refresh_history: deque = deque(maxlen=20)

    # ------------------------------------------------------------------ util
    def now(self) -> datetime:
        return datetime.now(self.tz).replace(tzinfo=None)

    def best_quote(self, code: str) -> tuple[float | None, float | None]:
        return self.book.get(code, (None, None))

    # ------------------------------------------------------------------ setup
    def _connect(self):
        import moomoo as mm
        self.mm = mm
        self.quote_ctx = mm.OpenQuoteContext(host=self.cfg.moomoo.host, port=self.cfg.moomoo.port)

        runner = self

        class KHandler(mm.CurKlineHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret == mm.RET_OK:
                    for _, row in data.iterrows():
                        runner.events.put(("kline", row["code"], row.to_dict()))
                return ret, data

        class QHandler(mm.StockQuoteHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret == mm.RET_OK:
                    for _, row in data.iterrows():
                        runner.events.put(("quote", row["code"], float(row["last_price"])))
                return ret, data

        class BHandler(mm.OrderBookHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret == mm.RET_OK:
                    bid = data["Bid"][0][0] if data.get("Bid") else None
                    ask = data["Ask"][0][0] if data.get("Ask") else None
                    runner.book[data["code"]] = (bid, ask)   # tuple の差し替えはアトミック
                return ret, data

        self.quote_ctx.set_handler(KHandler())
        self.quote_ctx.set_handler(QHandler())
        self.quote_ctx.set_handler(BHandler())

    def _lot_sizes(self, codes: list[str]) -> dict[str, float]:
        ret, df = self.quote_ctx.get_market_snapshot(codes)
        if ret != self.mm.RET_OK:
            if self.cfg.mode != "paper":
                raise SystemExit(f"get_market_snapshot 失敗 (相場権限・銘柄コードを確認): {df}")
            log.warning("get_market_snapshot 失敗 (%s)。売買単位は config の lot_size=%s を使います",
                        df, self.cfg.risk.lot_size)
            return {}
        out = {}
        for _, row in df.iterrows():
            out[row["code"]] = float(row["lot_size"] or 0) or self.cfg.risk.lot_size
            log.info("%s %s lot=%s last=%s bid=%s ask=%s", row["code"], row.get("name", ""), row["lot_size"],
                     row["last_price"], row.get("bid_price"), row.get("ask_price"))
        return out

    def _build_engines(self) -> None:
        cfg = self.cfg
        if cfg.mode == "paper":
            self.broker = PaperBroker(cfg, self.best_quote)
        else:
            from .moomoo_broker import MoomooBroker
            self.broker = MoomooBroker(cfg, self.best_quote)
            log.info("account: %s", self.broker.account_summary())
        self.risk = RiskManager(cfg.risk)
        self.sessions = TradingSessions.from_config(cfg.session)
        self.trade_logger = TradeLogger(cfg.log_dir, cfg.mode)
        self._add_engines(list(cfg.symbols))

    def _add_engines(self, codes: list[str]) -> None:
        cfg = self.cfg
        existing = self.broker.positions() if hasattr(self.broker, "positions") else {}
        lots = self._lot_sizes(codes)
        for code in codes:
            eng = SymbolEngine(code, cfg, create_strategy(cfg.strategy.name, cfg.strategy.params), self.risk,
                               self.broker, self.sessions, lot_size=lots.get(code), on_trade=self.trade_logger)
            if existing.get(code):
                # ボットが建てていない既存ポジションには触らない (誤決済防止)
                log.warning("%s: 既存ポジション %s 株があるため、この銘柄は売買しません", code, existing[code])
                self._blocked.add(code)
            self.engines[code] = eng
        self._apply_enabled()

    def _apply_enabled(self) -> None:
        """新規エントリーの可否 = 一時停止中でない & 既存ポジション銘柄でない & 入れ替え待ちでない。"""
        for code, eng in self.engines.items():
            eng.enabled = not (self.paused or code in self._blocked or code in self._retiring)

    @property
    def _session(self):
        """moomoo の Session 値 (米国株の時間外取引用)。RTH のときは None。"""
        name = self.cfg.session.us_session.upper()
        return None if name == "RTH" else getattr(self.mm.Session, name)

    def _warmup(self, eng: SymbolEngine) -> None:
        """過去足で指標を温める。get_cur_kline は購読済みであることが必要。"""
        mm = self.mm
        ktype = getattr(mm.KLType, _KTYPE[self.cfg.bar_minutes])
        n = self.cfg.warmup_bars + 1
        if self._session is None:
            ret, df = self.quote_ctx.get_cur_kline(eng.code, n, ktype, mm.AuType.QFQ)
            rows = [r.to_dict() for _, r in df.iterrows()] if ret == mm.RET_OK else None
        else:
            rows = self._history_rows(eng.code, ktype, n)
            ret, df = (mm.RET_OK, None) if rows is not None else (mm.RET_ERROR, "history failed")
        if rows is None:
            log.warning("%s warmup failed: %s", eng.code, df)
            return
        bars = [_row_to_bar(r) for r in rows]
        # 最後の 1 本は形成中なので除外し、プッシュ側で扱う
        if bars:
            self._pending_bar.setdefault(eng.code, rows[-1])
            bars = bars[:-1]
        eng.warmup(bars)
        self.bars.setdefault(eng.code, deque(maxlen=240)).extend(bars)
        if bars:
            self._last_bar_time[eng.code] = bars[-1].time
        log.info("%s warmed up with %d bars (last=%s, ready=%s)", eng.code, len(bars),
                 bars[-1].time if bars else None, eng.strategy.ready)

    def _history_rows(self, code: str, ktype, n: int) -> list[dict] | None:
        """時間外を含む直近 n 本 (get_cur_kline は時間外を含まないため履歴 API を使う)。"""
        mm = self.mm
        start = (self.now() - timedelta(days=4)).strftime("%Y-%m-%d")
        end = (self.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        rows, page = [], None
        while True:
            ret, df, page = self.quote_ctx.request_history_kline(
                code, start=start, end=end, ktype=ktype, max_count=1000, page_req_key=page,
                session=self._session)
            if ret != mm.RET_OK:
                log.warning("%s request_history_kline failed: %s", code, df)
                return None
            rows.extend(r.to_dict() for _, r in df.iterrows())
            if page is None:
                return rows[-n:]

    def _subtypes(self) -> list:
        mm = self.mm
        return [getattr(mm.SubType, _KTYPE[self.cfg.bar_minutes]), mm.SubType.QUOTE, mm.SubType.ORDER_BOOK]

    def _subscribe(self, codes: list[str] | None = None) -> None:
        mm = self.mm
        codes = list(codes if codes is not None else self.cfg.symbols)
        subs = self._subtypes()[:2]
        kw = {} if self._session is None else {"session": self._session}
        ret, err = self.quote_ctx.subscribe(codes, subs, subscribe_push=True, **kw)
        if ret != mm.RET_OK:
            raise SystemExit(f"subscribe 失敗 (相場権限・銘柄コードを確認): {err}")
        # 板が取れなければ現在値で代用する (paper のみ)
        ret, err = self.quote_ctx.subscribe(codes, [mm.SubType.ORDER_BOOK], subscribe_push=True)
        if ret != mm.RET_OK:
            if self.cfg.mode != "paper":
                raise SystemExit(f"板 (ORDER_BOOK) の購読に失敗: {err}")
            log.warning("板の購読に失敗 (%s)。paper の約定は現在値 + スリッページで計算します", err)
        log.info("subscribed %s session=%s", codes, self.cfg.session.us_session)

    def _unsubscribe(self, codes: list[str]) -> None:
        if not codes:
            return
        ret, err = self.quote_ctx.unsubscribe(codes, self._subtypes())
        if ret != self.mm.RET_OK:   # 購読から 1 分未満などで失敗しても売買には影響しない
            log.info("unsubscribe %s failed (無視): %s", codes, err)

    # ------------------------------------------------------------------ 銘柄の自動入れ替え
    def _schedule_refresh(self, minutes: float) -> None:
        self._next_refresh = _time.monotonic() + minutes * 60
        self.next_refresh_at = self.now() + timedelta(minutes=minutes)

    def _maybe_refresh(self, now: datetime) -> None:
        a = self.cfg.auto_symbols
        if not a.enabled:
            return
        idx = self.sessions.session_index(now)
        if idx != self._last_session_idx:
            self._last_session_idx = idx
            if idx is not None:
                # 新しい時間帯 (寄り付きなど) が始まった → 当日のデータが溜まるのを待ってから選び直す
                wait = max(self.cfg.session.no_entry_first_minutes, 5)
                log.info("時間帯が切り替わりました。%d 分後に銘柄を選び直します", wait)
                self._schedule_refresh(wait)
        self._retire_flat()
        if self._next_refresh is None or _time.monotonic() < self._next_refresh:
            return
        if idx is None:   # 市場が閉まっている間は選び直さない
            self._next_refresh = None
            self.next_refresh_at = None
            return
        self.refresh_symbols()
        if a.refresh_minutes:
            self._schedule_refresh(a.refresh_minutes)
        else:
            self._next_refresh = None
            self.next_refresh_at = None

    def refresh_symbols(self) -> None:
        """候補を選び直し、銘柄を入れ替える。ポジションのある銘柄は決済されるまで残す。"""
        from .screener import select
        current = [c for c in self.engines if c not in self._retiring]
        try:
            chosen = select(self.cfg, self.quote_ctx, self.mm, current)
        except Exception as e:
            log.warning("銘柄の選び直しに失敗 (今の銘柄を継続): %s", e)
            return
        self.last_refresh_at = self.now()
        add = [c for c in chosen if c not in self.engines]
        drop = [c for c in self.engines if c not in chosen]
        for c in chosen:
            self._retiring.discard(c)          # 入れ替え待ちだったが再び選ばれた
        for c in drop:
            self._retiring.add(c)
        if add:
            try:
                self._subscribe(add)
                self._add_engines(add)
                for c in add:
                    self._warmup(self.engines[c])
            except (Exception, SystemExit) as e:   # 追加に失敗しても今の銘柄で売買を続ける
                log.warning("銘柄の追加に失敗しました %s: %s", add, e)
                for c in add:
                    if c in self.engines and self.engines[c].position is None:
                        del self.engines[c]
                add = [c for c in add if c in self.engines]
        self._apply_enabled()
        self._retire_flat()
        self.cfg.symbols = list(self.engines)
        msg = f"銘柄を更新: {chosen}" + (f" 追加={add}" if add else "") + (f" 外す={drop}" if drop else "")
        log.info(msg if (add or drop) else f"銘柄を確認: 変更なし {chosen}")
        self.refresh_history.append({"time": str(self.last_refresh_at), "chosen": chosen, "add": add, "drop": drop})

    def _retire_flat(self) -> None:
        """入れ替え待ちの銘柄のうち、ポジションがなくなったものを外す。"""
        done = [c for c in self._retiring if c in self.engines and self.engines[c].position is None]
        for c in done:
            del self.engines[c]
            self._retiring.discard(c)
            self._pending_bar.pop(c, None)
            self._last_bar_time.pop(c, None)
            self.bars.pop(c, None)
            self.book.pop(c, None)
            log.info("%s を監視対象から外しました", c)
        if done:
            self._unsubscribe(done)
            self.cfg.symbols = list(self.engines)


    # ------------------------------------------------------------------ event handling
    def _on_kline(self, code: str, row: dict) -> None:
        """同じ time_key の更新が何度も届く。time_key が変わったら直前の足が確定。"""
        prev = self._pending_bar.get(code)
        new_t = parse_time(str(row["time_key"]))
        if prev is not None:
            prev_t = parse_time(str(prev["time_key"]))
            if new_t < prev_t:
                return          # 遅れて届いた古い足の再送は無視
            if new_t == prev_t:
                self._pending_bar[code] = row
                return
        self._pending_bar[code] = row
        if prev is None:
            return
        bar = _row_to_bar(prev)
        last = self._last_bar_time.get(code)
        if last is not None and bar.time <= last:
            return
        self._last_bar_time[code] = bar.time
        eng = self.engines[code]
        log.debug("%s bar %s O=%s H=%s L=%s C=%s V=%s", code, bar.time, bar.open, bar.high, bar.low, bar.close,
                  bar.volume)
        self.bars.setdefault(code, deque(maxlen=240)).append(bar)
        eng.on_bar(bar, intrabar_exits=False)

    def _drain(self, timeout: float = 1.0) -> None:
        try:
            first = self.events.get(timeout=timeout)
        except queue.Empty:
            return
        items = [first]
        while True:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                break
        latest_quote: dict[str, float] = {}
        for kind, code, payload in items:
            if kind == "cmd":
                self._on_command(payload)
                continue
            if code not in self.engines:
                continue
            if kind == "kline":
                self._on_kline(code, payload)
            elif kind == "quote":
                latest_quote[code] = payload   # 溜まった古い価格では判定しない
        now = self.now()
        for code, price in latest_quote.items():
            self.engines[code].check_price(price, now)

    def _on_command(self, cmd: str) -> None:
        if cmd == "flatten":
            now = self.now()
            for eng in self.engines.values():
                eng.force_flatten(now, "manual")
        elif cmd == "pause":
            self.paused = True
            self._apply_enabled()
            log.warning("新規エントリーを停止しました (保有中のポジションは通常どおり決済されます)")
        elif cmd == "resume":
            self.paused = False
            self._apply_enabled()
            log.info("新規エントリーを再開しました")
        elif cmd == "refresh":
            if self.cfg.auto_symbols.enabled:
                self.refresh_symbols()

    def request(self, cmd: str) -> None:
        """別スレッド (GUI) からの操作。エンジンはランナーのスレッドでだけ動かす。"""
        self.events.put(("cmd", None, cmd))

    def stop(self, *_):
        log.info("stop requested")
        self._stop = True

    def snapshot(self) -> dict:
        """GUI 表示用の状態。別スレッドから読むので失敗しても落ちないようにする。"""
        risk = getattr(self, "risk", None)
        symbols = []
        for code, eng in list(self.engines.items()):
            p = eng.position
            last = eng.last_price
            unreal = (last - p.entry_price) * p.qty if (p and last is not None) else 0.0
            bid, ask = self.best_quote(code)
            symbols.append({
                "code": code, "last": last, "bid": bid, "ask": ask, "enabled": eng.enabled,
                "retiring": code in self._retiring, "blocked": code in self._blocked,
                "ready": eng.strategy.ready,
                "position": None if p is None else {
                    "qty": p.qty, "entry": p.entry_price, "stop": p.stop, "target": p.target,
                    "entry_time": str(p.entry_time), "bars_held": p.bars_held, "unrealized": unreal},
                "bars": [[str(b.time), b.open, b.high, b.low, b.close] for b in list(self.bars.get(code, []))[-120:]],
                "trades": [{"direction": t.direction, "qty": t.qty, "entry_time": str(t.entry_time),
                            "entry": t.entry_price, "exit_time": str(t.exit_time), "exit": t.exit_price,
                            "pnl": t.pnl, "reason": t.reason} for t in eng.trades[-50:]],
            })
        return {
            "phase": self.phase, "error": self.error, "mode": self.cfg.mode, "market": self.cfg.market,
            "strategy": self.cfg.strategy.name, "auto": self.cfg.auto_symbols.enabled,
            "chosen": [c for c in self.engines if c not in self._retiring], "retiring": sorted(self._retiring),
            "paused": self.paused,
            "next_refresh": str(self.next_refresh_at)[:19] if self.next_refresh_at else None,
            "last_refresh": str(self.last_refresh_at)[:19] if self.last_refresh_at else None,
            "refresh_history": list(self.refresh_history)[-5:], "started_at": str(self.started_at) if self.started_at else None,
            "now": str(self.now()),
            "daily_pnl": risk.daily_pnl if risk else 0.0, "trades_today": risk.trades_today if risk else 0,
            "halted": risk.halted_reason if risk else None, "symbols": symbols,
        }

    def run(self) -> None:
        cfg = self.cfg
        log.info("=== start mode=%s market=%s symbols=%s strategy=%s ===", cfg.mode, cfg.market, cfg.symbols,
                 cfg.strategy.name)
        if cfg.mode == "live":
            log.warning("!!! 実口座 (REAL) で発注します。自己責任で運用してください !!!")
        self.started_at = self.now()
        self.phase = "connecting"
        try:
            self._connect()
            if cfg.auto_symbols.enabled:
                from .screener import select
                self.phase = "screening"
                cfg.symbols = select(cfg, self.quote_ctx, self.mm)
                self.last_refresh_at = self.now()
                log.info("自動選定した銘柄: %s", cfg.symbols)
            self._build_engines()
            self._subscribe()
            self.phase = "warming"
            for eng in self.engines.values():
                self._warmup(eng)
            if threading.current_thread() is threading.main_thread():
                signal.signal(signal.SIGINT, self.stop)
                signal.signal(signal.SIGTERM, self.stop)
            self.phase = "running"
            if cfg.auto_symbols.enabled:
                self._last_session_idx = self.sessions.session_index(self.now())
                if cfg.auto_symbols.refresh_minutes:
                    self._schedule_refresh(cfg.auto_symbols.refresh_minutes)
            last_status = 0.0
            while not self._stop:
                self._drain(timeout=1.0)
                now = self.now()
                for eng in list(self.engines.values()):
                    eng.on_clock(now)
                self._maybe_refresh(now)
                if _time.monotonic() - last_status > 60:
                    last_status = _time.monotonic()
                    self._log_status()
        except BaseException as e:   # SystemExit も GUI に表示したいので捕まえる
            self.phase = "error"
            self.error = str(e)
            log.error("停止しました: %s", e)
            if threading.current_thread() is threading.main_thread():
                raise
        finally:
            self._shutdown()

    def _log_status(self) -> None:
        r = getattr(self, "risk", None)
        pos = {c: (e.position.qty, e.position.entry_price) for c, e in self.engines.items() if e.position}
        if r:
            log.info("status: pnl=%.0f trades=%d positions=%s halted=%s", r.daily_pnl, r.trades_today, pos,
                     r.halted_reason)

    def _shutdown(self) -> None:
        now = self.now()
        for eng in self.engines.values():
            if eng.position is not None:
                log.warning("%s: 終了時にポジションを決済します", eng.code)
                eng.force_flatten(now, "shutdown")
        if self.quote_ctx is not None:
            self.quote_ctx.close()
        if self.broker is not None:
            self.broker.close()
        if self.phase != "error":
            self.phase = "stopped"
        log.info("=== stopped ===")


def _row_to_bar(row) -> Bar:
    return Bar(parse_time(str(row["time_key"])), float(row["open"]), float(row["high"]), float(row["low"]),
               float(row["close"]), float(row["volume"]))
