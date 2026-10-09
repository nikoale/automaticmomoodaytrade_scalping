"""moomoo の取引パスワードの保管 (実口座のロック解除 unlock_trade 用)。

おすすめ: ② キーチェーン (Mac の Keychain。keyring ライブラリ経由で OS の保管庫に保存)
もう一つ: ① この起動中だけ使う (メモリ上だけ。画面を閉じると消える)
VPS (Linux) などキーチェーンが使えない環境では、環境変数 MOOMOO_TRADE_PASSWORD も読む。

パスワードは画面・ログ・ファイルには一切出さない。使う順番: ① メモリ → ② キーチェーン → 環境変数。
模擬口座 (SIMULATE) ではロック解除そのものが不要 (SDK のソース: 「模拟交易不需要解锁」)。
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

SERVICE = "moomoo-swing-bot"
USERNAME = "moomoo_trade_password"
ENV_VAR = "MOOMOO_TRADE_PASSWORD"


def _keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def keychain_available() -> tuple[bool, str]:
    kr = _keyring()
    if kr is None:
        return False, "keyring ライブラリが入っていません"
    backend = kr.get_keyring()
    name = type(backend).__module__ + "." + type(backend).__name__
    if "fail" in name.lower() or getattr(backend, "priority", 1) <= 0:
        return False, "この環境ではキーチェーンが使えません (Mac 以外では環境変数を使ってください)"
    return True, name


class PasswordStore:
    def __init__(self):
        self._memory: str | None = None

    # ① この起動中だけ
    def use_for_session(self, pw: str) -> None:
        if not pw:
            raise ValueError("パスワードが空です")
        self._memory = pw
        log.info("取引パスワードを「この起動中だけ」使うように設定しました")

    def forget_session(self) -> None:
        self._memory = None

    # ② キーチェーン
    def save_keychain(self, pw: str) -> None:
        if not pw:
            raise ValueError("パスワードが空です")
        ok, why = keychain_available()
        if not ok:
            raise RuntimeError(why)
        _keyring().set_password(SERVICE, USERNAME, pw)
        log.info("取引パスワードをキーチェーンに保存しました")

    def delete_keychain(self) -> None:
        ok, why = keychain_available()
        if not ok:
            raise RuntimeError(why)
        kr = _keyring()
        try:
            kr.delete_password(SERVICE, USERNAME)
        except kr.errors.PasswordDeleteError:
            pass
        log.info("キーチェーンから取引パスワードを削除しました")

    def _from_keychain(self) -> str | None:
        ok, _ = keychain_available()
        if not ok:
            return None
        try:
            return _keyring().get_password(SERVICE, USERNAME)
        except Exception as e:  # noqa: BLE001 - キーチェーンの拒否など
            log.warning("キーチェーンを読めませんでした: %s", e.__class__.__name__)
            return None

    def get(self) -> tuple[str | None, str | None]:
        """(パスワード, どこから) を返す。"""
        if self._memory:
            return self._memory, "session"
        pw = self._from_keychain()
        if pw:
            return pw, "keychain"
        pw = os.environ.get(ENV_VAR)
        if pw:
            return pw, "env"
        return None, None

    def status(self) -> dict:
        """画面表示用。パスワードそのものは含めない。"""
        ok, why = keychain_available()
        saved = bool(self._from_keychain()) if ok else False
        _, src = self.get()
        return {"source": src, "keychain_available": ok, "keychain_note": "" if ok else why,
                "keychain_saved": saved, "session_set": bool(self._memory), "env_set": bool(os.environ.get(ENV_VAR))}


def verify(cfg, password: str) -> tuple[bool, str]:
    """実口座のロック解除ができるか確かめ、すぐにロックし直す。注文は出さない。"""
    import moomoo as mm
    ctx = mm.OpenSecTradeContext(filter_trdmarket=mm.TrdMarket.US, host=cfg["moomoo"]["host"],
                                 port=cfg["moomoo"]["port"],
                                 security_firm=getattr(mm.SecurityFirm, cfg["moomoo"]["security_firm"]))
    try:
        ret, data = ctx.unlock_trade(password=password)
        if ret != mm.RET_OK:
            from .broker import GUI_UNLOCK_HINT, gui_unlock_only
            if gui_unlock_only(data):
                return False, f"GUI版 OpenD のため、アプリからはロック解除できません（パスワードが違うわけではありません）。{GUI_UNLOCK_HINT}"
            return False, f"ロック解除できませんでした (パスワードが違う可能性): {data}"
        ctx.unlock_trade(password=password, is_unlock=False)      # 確認だけなので、すぐにロックし直す
        return True, "パスワードは正しいです (ロック解除できることを確認し、すぐにロックし直しました)"
    finally:
        ctx.close()
