"""接続チェックと銘柄検索 (CLI と GUI で共通)。"""
from __future__ import annotations

from typing import Callable

from .config import Config
from .live import _KTYPE, _row_to_bar
from .models import Bar


def _mm():
    try:
        import moomoo as mm
    except ImportError as e:
        raise RuntimeError("moomoo-api がインストールされていません (pip install moomoo-api)") from e
    return mm


def run_check(cfg: Config, out: Callable[[str], None]) -> bool:
    """接続・相場権限・現在値・板・直近の足を out に書き出す。市場時間外でも実行できる。"""
    mm = _mm()
    out(f"OpenD {cfg.moomoo.host}:{cfg.moomoo.port} に接続します...")
    ctx = mm.OpenQuoteContext(host=cfg.moomoo.host, port=cfg.moomoo.port)
    try:
        ret, st = ctx.get_global_state()
        if ret != mm.RET_OK:
            out(f"❌ OpenD に接続できません: {st}\n→ OpenD が起動してログイン済みか確認してください")
            return False
        qot = str(st.get("qot_logined")) in ("1", "True")
        trd = str(st.get("trd_logined")) in ("1", "True")
        out(f"[ログイン] 相場サーバ={'OK' if qot else 'NG'}  取引サーバ={'OK' if trd else 'NG'}")
        out(f"[市場状態] US={st.get('market_us')}")

        ret, info = ctx.get_user_info()
        if ret == mm.RET_OK and isinstance(info, dict):
            out(f"[相場権限] 米国株={info.get('us_qot_right')}")

        ret, snap = ctx.get_market_snapshot(cfg.symbols)
        if ret != mm.RET_OK:
            out(f"[スナップショット] 取得できません: {snap}")
        else:
            out("[スナップショット]")
            for _, r in snap.iterrows():
                out(f"  {r['code']}  {str(r.get('name', ''))[:24]}  現在値={r['last_price']}  "
                    f"買気配={r.get('bid_price')}  売気配={r.get('ask_price')}  単位={r['lot_size']}  "
                    f"更新={r.get('update_time')}")

        session = cfg.session.us_session.upper()
        kw = {} if session == "RTH" else {"session": getattr(mm.Session, session)}
        ktype = _KTYPE[cfg.bar_minutes]
        ret, err = ctx.subscribe(cfg.symbols, [getattr(mm.SubType, ktype)], **kw)
        if ret != mm.RET_OK:
            out(f"❌ 足の購読に失敗: {err}\n→ この市場の相場権限がないか、銘柄コードが違います (銘柄検索で確認)")
            return False
        ret, err = ctx.subscribe(cfg.symbols, [mm.SubType.ORDER_BOOK])
        has_book = ret == mm.RET_OK
        if not has_book:
            out(f"[板] 購読できません ({err})。paper は現在値で約定計算します")
        for code in cfg.symbols:
            ret, df = ctx.get_cur_kline(code, 5, getattr(mm.KLType, ktype), mm.AuType.QFQ)
            out(f"[{code} 直近の{cfg.bar_minutes}分足]")
            if ret == mm.RET_OK:
                for _, r in df.iterrows():
                    out(f"  {r['time_key']}  始={r['open']} 高={r['high']} 安={r['low']} 終={r['close']} 出来高={r['volume']}")
            else:
                out(f"  取得失敗: {df}")
            if has_book:
                ret, ob = ctx.get_order_book(code, num=3)
                if ret == mm.RET_OK:
                    asks = [(p, v) for p, v, *_ in ob.get("Ask", [])]
                    bids = [(p, v) for p, v, *_ in ob.get("Bid", [])]
                    out(f"  板 売: {asks}\n     買: {bids}")
        out("✅ OpenD との接続と相場取得は正常です。")
        if cfg.mode in ("simulate", "live"):
            return _check_account(cfg, out)
        return True
    finally:
        ctx.close()


def search_symbols(cfg: Config, query: str | None = None, limit: int = 50) -> tuple[list[dict], int]:
    """moomoo の米国株一覧からコード・名前で検索する。"""
    mm = _mm()
    ctx = mm.OpenQuoteContext(host=cfg.moomoo.host, port=cfg.moomoo.port)
    try:
        ret, df = ctx.get_stock_basicinfo(mm.Market.US, mm.SecurityType.STOCK)
        if ret != mm.RET_OK:
            raise RuntimeError(f"get_stock_basicinfo 失敗: {df}")
        if query:
            q = query.upper()
            df = df[df["code"].str.upper().str.contains(q, regex=False)
                    | df["name"].astype(str).str.upper().str.contains(q, regex=False)]
        rows = [{"code": r["code"], "name": str(r["name"]), "lot_size": r["lot_size"]}
                for _, r in df.head(limit).iterrows()]
        return rows, len(df)
    finally:
        ctx.close()


def fetch_history(cfg: Config, code: str, start: str, end: str, bar_minutes: int = 1) -> list[Bar]:
    """moomoo から過去の足を取得する (start/end は YYYY-MM-DD)。"""
    mm = _mm()
    ctx = mm.OpenQuoteContext(host=cfg.moomoo.host, port=cfg.moomoo.port)
    try:
        ktype = getattr(mm.KLType, _KTYPE[bar_minutes])
        session = cfg.session.us_session.upper()
        kw = {} if session == "RTH" else {"session": getattr(mm.Session, session)}
        bars: list[Bar] = []
        page = None
        while True:
            ret, df, page = ctx.request_history_kline(code, start=start, end=end, ktype=ktype, max_count=1000,
                                                      page_req_key=page, **kw)
            if ret != mm.RET_OK:
                raise RuntimeError(f"request_history_kline 失敗: {df}")
            bars.extend(_row_to_bar(r) for _, r in df.iterrows())
            if page is None:
                return bars
    finally:
        ctx.close()


def screen_preview(cfg: Config) -> list[dict]:
    """自動選定の候補ランキング (GUI の「候補を見る」用)。"""
    from .screener import screen
    mm = _mm()
    ctx = mm.OpenQuoteContext(host=cfg.moomoo.host, port=cfg.moomoo.port)
    try:
        return screen(cfg, ctx, mm)
    finally:
        ctx.close()


def _check_account(cfg: Config, out: Callable[[str], None]) -> bool:
    """模擬口座 / 実口座の残高・保有株・残っている注文を表示する (発注はしない)。"""
    from .moomoo_broker import MoomooBroker
    label = "moomoo 模擬口座" if cfg.mode == "simulate" else "★ 実口座"
    out(f"\n[{label}]")
    try:
        broker = MoomooBroker(cfg)
    except (Exception, SystemExit) as e:
        out(f"❌ 口座に接続できません: {e}")
        return False
    try:
        info = broker.account_summary()
        out(f"  総資産={info.get('total_assets')}  現金={info.get('cash')}  "
            f"買付余力={info.get('usd_net_cash_power') or info.get('power')} (USD)")
        pos = broker.positions()
        out("  保有株: " + (", ".join(f"{c} {q:g}株" for c, q in pos.items()) if pos else "なし"))
        if pos:
            out("  ※ 保有中の銘柄はボットが売買しません (手動のポジションを守るため)")
        orders = broker.open_bot_orders()
        if orders:
            out("  ボットが前回出した注文: " + ", ".join(f"{o['code']} {o['remark']}" for o in orders))
        out(f"✅ {label}に接続できました。")
        return True
    except Exception as e:
        out(f"❌ 口座情報の取得に失敗: {e}")
        return False
    finally:
        broker.close()
