"""フェーズ 4: 毎日の自動実行 (スケジューラー)。

日本時間で動き、米国の夏時間・冬時間・祝日は calendar_us が自動で扱う。

  引け後の処理 (trade_close) : 毎日 schedule.daily_run (既定 7:30)。直近の米国の取引日の引けの後
  寄り付き後の処理 (trade_open): 米国の取引日の寄り付き + open_run_delay_min 分 (夏 22:30 / 冬 23:30 + 数分)
                                 寄り付きから open_run_max_late_min 分を過ぎたら、その日は出さない (遅すぎる約定を避ける)
  週次スクリーナー (screen)    : 毎週 weekly_screen_day の weekly_screen_time (既定 土曜 9:00)

各処理は「どの取引日 / どの週の分を済ませたか」を data.dir/runner_state.json に記録するので、
同じ分を 2 回は動かさない。Mac がスリープしていて時刻を過ぎていても、起きたときに (間に合うものは) 実行する。
処理がエラーで終わっても、同じ分は繰り返さない (発注処理は自分で「発注停止」になり、解除は人がする)。

使い方:
  画面 (GUI) の「自動実行」をオンにする → 画面を開いている間 (start.command のウィンドウがある間) 動く
  VPS などでは python -m swing run (常駐。systemd の例は README)
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from datetime import date, datetime, time, timedelta

from . import calendar_us

log = logging.getLogger(__name__)

DAYS = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6}
LABELS = {"trade_close": "引け後の処理", "trade_open": "寄り付き後の処理", "screen": "週次スクリーナー"}


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def close_run_time(cfg, session: date) -> datetime:
    """取引日 session の分の「引け後の処理」の時刻 (日本時間)。引けより後の最初の daily_run。"""
    _, close_jst = calendar_us.session_times_jst(session)
    t = datetime.combine(close_jst.date(), _hm(cfg["schedule"]["daily_run"]), calendar_us.TOKYO)
    return t if t >= close_jst else t + timedelta(days=1)


def weekly_time(cfg, now: datetime) -> datetime:
    """now 以前で直近の「週次スクリーナー」の時刻。"""
    sc = cfg["schedule"]
    wd = DAYS[sc["weekly_screen_day"]]
    d = now.date() - timedelta(days=(now.weekday() - wd) % 7)
    t = datetime.combine(d, _hm(sc["weekly_screen_time"]), calendar_us.TOKYO)
    return t if t <= now else t - timedelta(days=7)


def due(cfg, now: datetime, done: dict) -> list[tuple[str, str]]:
    """今やるべき処理 [(名前, 済んだ印)]。順番は 引け後 → 週次 → 寄り付き後。"""
    sc = cfg["schedule"]
    out = []
    s = calendar_us.last_completed_session(now)
    if now >= close_run_time(cfg, s) and done.get("trade_close") != s.isoformat():
        out.append(("trade_close", s.isoformat()))
    w = weekly_time(cfg, now)
    if now - w < timedelta(days=2) and done.get("screen") != w.date().isoformat():
        out.append(("screen", w.date().isoformat()))
    d = now.astimezone(calendar_us.NY).date()
    if calendar_us.is_trading_day(d):
        o, c = calendar_us.session_times_jst(d)
        start = o + timedelta(minutes=sc["open_run_delay_min"])
        late = o + timedelta(minutes=sc["open_run_max_late_min"])
        if start <= now < min(late, c) and done.get("trade_open") != d.isoformat():
            out.append(("trade_open", d.isoformat()))
    return out


def upcoming(cfg, now: datetime) -> list[dict]:
    """画面表示用: 次の予定 (それぞれ 1 件)。"""
    sc = cfg["schedule"]
    res = []
    s = calendar_us.last_completed_session(now)
    t = close_run_time(cfg, s)
    if t <= now:
        t = close_run_time(cfg, calendar_us.next_trading_day(s))
    res.append({"name": "trade_close", "label": LABELS["trade_close"], "time": t})
    d = now.astimezone(calendar_us.NY).date()
    if not calendar_us.is_trading_day(d) or now >= calendar_us.session_times_jst(d)[0] + \
            timedelta(minutes=sc["open_run_delay_min"]):
        d = calendar_us.next_trading_day(d)
    res.append({"name": "trade_open", "label": LABELS["trade_open"],
                "time": calendar_us.session_times_jst(d)[0] + timedelta(minutes=sc["open_run_delay_min"])})
    res.append({"name": "screen", "label": LABELS["screen"], "time": weekly_time(cfg, now) + timedelta(days=7)})
    res.sort(key=lambda x: x["time"])
    wd = "月火水木金土日"
    return [{**x, "iso": x["time"].isoformat(), "time": x["time"].strftime("%m/%d") + f"({wd[x['time'].weekday()]}) "
             + x["time"].strftime("%H:%M")} for x in res]


def notify(cfg, title: str, msg: str) -> None:
    """Mac の通知 (発注停止など)。ほかの OS では何もしない。"""
    if not cfg["schedule"]["notify"] or sys.platform != "darwin":
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')[:200]      # noqa: E731
    try:
        subprocess.run(["osascript", "-e", f'display notification "{esc(msg)}" with title "{esc(title)}"'],
                       timeout=10, check=False, capture_output=True)
    except Exception:  # noqa: BLE001
        pass


class Scheduler:
    """tick() を定期的に呼ぶと、時刻が来た処理を run(name) で実行する (終わるまで待つ)。

    run(name) -> "ok" | "error" | "busy"
      ok    : 終わった (発注停止になった場合も含む。停止の解除は人がする)
      error : 始められなかった・途中で失敗した (OpenD に接続できないなど) → retry_min 分後にもう一度。
              retry_count 回失敗したら、その分はあきらめる
      busy  : 画面で別の処理が実行中 → 次の tick でもう一度
    """

    def __init__(self, cfg_fn, run, now=None):
        self.cfg_fn, self.run = cfg_fn, run
        self.now = now or (lambda: datetime.now(calendar_us.TOKYO))
        self.lock = threading.Lock()
        self.fails: dict[tuple, tuple[int, datetime]] = {}     # (名前, 印) → (失敗回数, 次に試す時刻)
        self.last: dict[str, dict] = {}                        # 画面表示用: 最後の結果

    def _path(self):
        return self.cfg_fn().path("runner_state.json")

    def done(self) -> dict:
        try:
            return json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _mark(self, name: str, key: str) -> None:
        d = self.done()
        d[name] = key
        d[f"{name}_at"] = self.now().isoformat(timespec="seconds")
        p = self._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")

    def tick(self) -> list[str]:
        with self.lock:
            cfg, now = self.cfg_fn(), self.now()
            sc = cfg["schedule"]
            ran = []
            for name, key in due(cfg, now, self.done()):
                n, nxt = self.fails.get((name, key), (0, now))
                if now < nxt:
                    continue
                log.info("[自動実行] %s (%s の分) を始めます", LABELS[name], key)
                r = self.run(name)
                if r == "busy":
                    log.info("[自動実行] 別の処理が実行中なので、少し後でもう一度")
                    break
                self.last[name] = {"key": key, "result": r, "time": self.now().strftime("%m/%d %H:%M")}
                if r == "error" and n + 1 < sc["retry_count"]:
                    self.fails[(name, key)] = (n + 1, self.now() + timedelta(minutes=sc["retry_min"]))
                    log.warning("[自動実行] %s が失敗しました。%d 分後にもう一度試します (%d/%d)",
                                LABELS[name], sc["retry_min"], n + 1, sc["retry_count"])
                    continue
                if r == "error":
                    log.error("[自動実行] %s が %d 回失敗したので、%s の分はあきらめます", LABELS[name], n + 1, key)
                    notify(cfg, "スイング bot: 自動実行の失敗", f"{LABELS[name]} ({key}) が {n + 1} 回失敗しました")
                self.fails.pop((name, key), None)
                self._mark(name, key)
                ran.append(name)
            return ran


# ---------------------------------------------------------------- 常駐 (VPS など。画面なし)
def run_forever(cfg_fn, confirm=None, stop: threading.Event | None = None) -> None:
    """python -m swing run。処理は 1 つずつ同じスレッドで実行する。"""
    from . import executor
    from .broker import resolve_env
    from .screener import run_weekly
    cfg = cfg_fn()
    env, _ = resolve_env(cfg, confirm)              # 本番口座なら起動時に 1 回だけ人が確認する
    confirmed = (lambda: True) if env == "REAL" else None
    log.warning("[自動実行] 開始 (%s)。次の予定: %s", "本番口座" if env == "REAL" else "模擬口座",
                ", ".join(f"{x['label']} {x['time']}" for x in upcoming(cfg, datetime.now(calendar_us.TOKYO))))

    def run(name: str) -> str:
        c = cfg_fn()
        try:
            if name == "screen":
                run_weekly(c)
            else:
                with executor.Session(c, confirm=confirmed) as ex:
                    s = ex.run_close() if name == "trade_close" else ex.run_open()
                if s["halt"]["on"]:
                    notify(c, "スイング bot: 発注停止", s["halt"]["reason"])
        except Exception as e:  # noqa: BLE001 - 常駐は止めない (発注は executor 側で停止になる)
            log.error("[自動実行] %s でエラー: %s", LABELS[name], e)
            return "error"
        return "ok"

    sch = Scheduler(cfg_fn, run)
    stop = stop or threading.Event()
    while not stop.is_set():
        sch.tick()
        stop.wait(cfg["schedule"]["tick_sec"])
