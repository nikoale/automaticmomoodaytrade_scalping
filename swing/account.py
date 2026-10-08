"""口座の残高・保有株を読む (読むだけ。注文・訂正・取消は一切しない)。

関数名・列名は公式 SDK moomoo-api 10.11 のソースで確認 (実機では未検証):
  OpenSecTradeContext(filter_trdmarket=TrdMarket.US, security_firm=SecurityFirm.FUTUJP)
  get_acc_list()                       口座一覧 (acc_id, trd_env, acc_type, ...)
  accinfo_query(trd_env, acc_id, refresh_cache, currency)   資産 (currency=USD / JPY で 2 回)
  position_list_query(trd_env, acc_id, position_market, currency)   保有株

取引パスワードによるロック解除 (unlock_trade) はしない。照会だけならロック解除は不要の想定 (要確認)。
金額はログ・ファイルに残さない (画面に表示するだけ)。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime

from . import calendar_us

log = logging.getLogger(__name__)

# 画面に出す資産の項目 (accinfo_query の列名 → 表示名)
SUMMARY_FIELDS = [
    ("total_assets", "総資産"),
    ("cash", "現金"),
    ("market_val", "株の評価額"),
    ("power", "買付余力"),
    ("avl_withdrawal_cash", "出金可能額"),
    ("unrealized_pl", "含み損益"),
    ("realized_pl", "実現損益"),
]


def _num(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


class AccountReader:
    def __init__(self, cfg, env: str = "REAL"):
        import moomoo as mm
        if env not in ("REAL", "SIMULATE"):
            raise ValueError("env は REAL / SIMULATE")
        self.mm, self.cfg = mm, cfg
        self.env = getattr(mm.TrdEnv, env)
        self.env_name = env
        self.ctx = mm.OpenSecTradeContext(filter_trdmarket=mm.TrdMarket.US, host=cfg["moomoo"]["host"],
                                          port=cfg["moomoo"]["port"],
                                          security_firm=getattr(mm.SecurityFirm, cfg["moomoo"]["security_firm"]))

    def close(self) -> None:
        self.ctx.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def accounts(self) -> list[dict]:
        ret, df = self.ctx.get_acc_list()
        if ret != self.mm.RET_OK:
            raise RuntimeError(f"口座一覧 (get_acc_list) を取得できません: {df}")
        rows = [r.to_dict() for _, r in df.iterrows() if str(r.get("trd_env")) == str(self.env)]
        # 米国株の取引権限がある口座を先に
        us = [r for r in rows if "US" in str(r.get("trdmarket_auth", ""))]
        return us + [r for r in rows if r not in us]

    def _accinfo(self, acc_id: int, currency) -> dict:
        ret, df = self.ctx.accinfo_query(trd_env=self.env, acc_id=acc_id, refresh_cache=True, currency=currency)
        if ret != self.mm.RET_OK:
            raise RuntimeError(f"資産 (accinfo_query) を取得できません: {df}")
        if df.empty:
            raise RuntimeError("資産のデータが空です")
        return df.iloc[0].to_dict()

    def snapshot(self) -> dict:
        mm, cfg = self.mm, self.cfg
        accs = self.accounts()
        if not accs:
            raise RuntimeError(f"{'実口座' if self.env_name == 'REAL' else '模擬口座'} の米国株口座が見つかりません")
        want = int(cfg["moomoo"].get("acc_id") or 0)
        acc = next((a for a in accs if int(a["acc_id"]) == want), accs[0]) if want else accs[0]
        acc_id = int(acc["acc_id"])
        usd = self._accinfo(acc_id, mm.Currency.USD)
        jpy = self._accinfo(acc_id, mm.Currency.JPY)
        # 為替: 同じ口座の総資産を ドル建て / 円建て で取った比 (moomoo の換算レートに合わせる)
        tu, tj = _num(usd.get("total_assets")), _num(jpy.get("total_assets"))
        if tu and tj and tu > 0 and sane_fx(cfg, tj / tu):
            fx, fx_src = tj / tu, "口座の総資産 (円 / ドル) から計算"
        elif tu and tj and tu > 0:
            fx, fx_src = float(cfg.account["fx_rate_jpy_per_usd"]), \
                f"設定値 (口座の円 / ドルの比が {tj / tu:.2f} でおかしいため。模擬口座で起きます)"
        else:
            fx, fx_src = float(cfg.account["fx_rate_jpy_per_usd"]), "設定値 (口座から計算できなかったため)"
        summary = {k: {"label": lab, "usd": _num(usd.get(k)), "jpy": _num(jpy.get(k))} for k, lab in SUMMARY_FIELDS}
        ret, pdf = self.ctx.position_list_query(trd_env=self.env, acc_id=acc_id, position_market=mm.TrdMarket.US,
                                                currency=mm.Currency.USD, refresh_cache=True)
        if ret != mm.RET_OK:
            raise RuntimeError(f"保有株 (position_list_query) を取得できません: {pdf}")
        positions = []
        for _, r in pdf.iterrows():
            qty = _num(r.get("qty")) or 0.0
            if not qty:
                continue
            mv, pl = _num(r.get("market_val")), _num(r.get("pl_val"))
            positions.append({
                "code": r.get("code"), "name": r.get("stock_name"), "qty": qty,
                "side": r.get("position_side"),
                "cost_usd": _num(r.get("average_cost")) or _num(r.get("cost_price")),
                "price_usd": _num(r.get("nominal_price")),
                "market_val_usd": mv, "market_val_jpy": mv * fx if mv is not None else None,
                "pl_usd": pl, "pl_jpy": pl * fx if pl is not None else None,
                "pl_ratio": _num(r.get("pl_ratio")),
                "today_pl_usd": _num(r.get("today_pl_val")),
            })
        log.info("口座を読み込みました (%s・保有 %d 銘柄)", "実口座" if self.env_name == "REAL" else "模擬口座",
                 len(positions))                         # 金額はログに出さない
        return {"env": self.env_name, "acc_id": acc_id, "acc_type": acc.get("acc_type"), "fx": fx, "fx_source": fx_src,
                "summary": summary, "positions": positions,
                "time": datetime.now(calendar_us.TOKYO).strftime("%Y-%m-%d %H:%M:%S")}


def sane_fx(cfg, fx: float | None) -> float | None:
    """口座から計算した為替 (円/ドル) が config の範囲内ならその値、外なら None。"""
    lo, hi = cfg.account["fx_sane_range"]
    if fx is None or not (lo <= fx <= hi):
        if fx is not None:
            log.warning("口座から計算した為替 %.4f 円/ドル は範囲 (%s〜%s) の外なので使いません (設定値を使います)", fx, lo, hi)
        return None
    return fx


def effective_capital(cfg, account_total_usd: float | None = None, fx: float | None = None) -> dict:
    """ボットが使う資金 (USD と円)。

    account.capital_source:
      config  → 設定の運用資金 (capital_jpy) だけを使う
      account → 「設定の運用資金」と「口座の総資産」の小さい方 (口座に多くあっても設定額までしか使わない)
    fx: 円/ドル。口座を読み込んでいれば口座の換算レート、なければ設定値。
    """
    fx = sane_fx(cfg, fx) or float(cfg.account["fx_rate_jpy_per_usd"])
    conf_jpy = float(cfg.account["capital_jpy"])
    conf_usd = conf_jpy / fx
    src = cfg.account["capital_source"]
    if src == "account" and account_total_usd is not None:
        use = min(conf_usd, account_total_usd)
        why = "口座の総資産の方が少ないため、口座の総資産" if account_total_usd < conf_usd else "設定の運用資金（上限）"
    else:
        use = conf_usd
        why = "設定の運用資金" + ("（口座を読み込むと、口座の総資産と比べます）" if src == "account" else "")
    return {"usd": use, "jpy": use * fx, "config_usd": conf_usd, "config_jpy": conf_jpy,
            "account_usd": account_total_usd, "account_jpy": account_total_usd * fx if account_total_usd is not None else None,
            "fx": fx, "source": src, "reason": why}
