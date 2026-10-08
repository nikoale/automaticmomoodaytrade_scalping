"""本番口座の少額ベータ (画面から本番口座で発注するための二重ロックと条件)。

二重ロック (指示書: config の flag + 起動時の明示的な確認):
  1. 許可 (data.dir/real_beta.json の allowed)  … 画面で決まった文を入力して 1 回だけ。解除すると模擬口座に戻る
  2. この起動中のロック解除 (メモリだけ)        … アプリを起動するたびに、決まった文を入力し直す
  両方そろったときだけ、本番口座 (REAL) で発注する。どちらかが欠けると本番の注文は出ない。
  許可したまま起動し直すと、ロック解除までは模擬口座の注文も出さない (本番のつもりで模擬が動くのを防ぐ)。

許可する前の条件 (コードで強制する):
  - 模擬口座の「発注機能の確認」を米国の取引時間中に行い、逆指値まですべて通っていること
    (ユーザーが「確認を省略して許可」を選んだときは省略する。その場合も、逆指値が入らなければ成行で売って止まる)
  - 取引パスワードが設定されていて、ロック解除できること (ロック解除のときに確認)
  - 本番の資金・1 注文の上限が config の real_beta.capital_jpy_hard_max 以下

本番の資金は real_beta.capital_jpy (画面で変更可) を上限にする (口座の総資産の方が少なければそちら)。
1 注文の金額は real_beta.max_order_jpy を超えないよう株数を減らす。
"""
from __future__ import annotations

import copy
import json
import logging

from .broker import REAL_CONFIRM_PHRASE

log = logging.getLogger(__name__)

PHRASE = REAL_CONFIRM_PHRASE


def _path(cfg):
    return cfg.path("real_beta.json")


def load(cfg) -> dict:
    rb = cfg["real_beta"]
    d = {"allowed": False, "capital_jpy": rb["capital_jpy"], "max_order_jpy": rb["max_order_jpy"], "allowed_at": None}
    try:
        d.update(json.loads(_path(cfg).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return d


def save(cfg, d: dict) -> dict:
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def check_amounts(cfg, capital_jpy, max_order_jpy) -> tuple[int, int]:
    hard = int(cfg["real_beta"]["capital_jpy_hard_max"])
    cap, mo = int(capital_jpy), int(max_order_jpy)
    if not (10_000 <= cap <= hard):
        raise ValueError(f"本番の資金は 1 万円〜{hard:,} 円で指定してください")
    if not (1_000 <= mo <= cap):
        raise ValueError("1 注文の上限は 1,000 円〜本番の資金 までで指定してください")
    return cap, mo


def capability_ok(cfg) -> tuple[bool, str]:
    """模擬口座で、取引時間中の発注機能の確認 (逆指値・訂正・取消まで) が通っているか。"""
    p = cfg.path(cfg["executor"]["state_dir"], "capability_SIMULATE.json")
    try:
        c = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "模擬口座の「発注機能の確認」をまだしていません"
    if not c.get("regular_hours"):
        return False, "「発注機能の確認」を米国の取引時間中（夜）にもう一度してください（逆指値の確認がまだです）"
    if not c.get("ok"):
        bad = [s["name"] for s in c.get("steps", []) if not s.get("ok")]
        return False, "「発注機能の確認」で通らないものがあります: " + "、".join(bad)
    return True, f"確認済み（{str(c.get('time', ''))[:16].replace('T', ' ')}）"


def preconditions(cfg, password_set: bool, beta: dict | None = None) -> list[dict]:
    ok, why = capability_ok(cfg)
    if not ok and (beta or {}).get("skip_sim_check"):
        ok, why = True, "省略（あなたの判断で、模擬口座での確認なしに許可）"
    rows = [{"name": "模擬口座で逆指値まで確認", "ok": ok, "detail": why, "skippable": True}]
    rows.append({"name": "取引パスワード", "ok": bool(password_set),
                 "detail": "設定済み" if password_set else "「取引パスワード」で保存してください"})
    if not cfg["real_beta"]["require_simulate_check"]:
        rows[0]["ok"], rows[0]["detail"] = True, "確認を省略する設定 (real_beta.require_simulate_check: false)"
    return rows


def real_cfg(cfg, beta: dict):
    """本番口座用の設定 (資金を本番の上限に、1 注文の上限を付ける)。元の cfg は変えない。"""
    c = copy.deepcopy(cfg)
    c["moomoo"]["trd_env"] = "REAL"
    c["moomoo"]["allow_real"] = bool(beta.get("allowed"))
    c["account"]["capital_jpy"] = int(beta["capital_jpy"])
    c["executor"]["max_order_jpy"] = int(beta["max_order_jpy"])
    return c
