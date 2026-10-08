"""取引パスワード: ① この起動中だけ / ② キーチェーン / 環境変数、とロック解除の確認。"""
import json
import sys
import threading
import time
import types
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml

SECRET = "s3cr3t-PW-123"


def fake_keyring(available=True):
    store = {}

    class Backend:
        priority = 1 if available else 0

    if not available:
        Backend.__module__ = "keyring.backends.fail"
    errors = types.SimpleNamespace(PasswordDeleteError=KeyError)
    kr = types.ModuleType("keyring")
    kr.get_keyring = lambda: Backend()
    kr.set_password = lambda s, u, p: store.__setitem__((s, u), p)
    kr.get_password = lambda s, u: store.get((s, u))

    def delete(s, u):
        if (s, u) not in store:
            raise KeyError
        del store[(s, u)]
    kr.delete_password = delete
    kr.errors = errors
    kr._store = store
    return kr


@pytest.fixture
def kr(monkeypatch):
    monkeypatch.delenv("MOOMOO_TRADE_PASSWORD", raising=False)

    def go(available=True):
        k = fake_keyring(available)
        monkeypatch.setitem(sys.modules, "keyring", k)
        return k
    return go


def test_priority_session_keychain_env(kr, monkeypatch):
    from swing.passwords import PasswordStore
    kr()
    ps = PasswordStore()
    assert ps.get() == (None, None)
    monkeypatch.setenv("MOOMOO_TRADE_PASSWORD", "from-env")
    assert ps.get() == ("from-env", "env")
    ps.save_keychain("from-keychain")
    assert ps.get() == ("from-keychain", "keychain")
    ps.use_for_session("from-session")
    assert ps.get() == ("from-session", "session")
    ps.forget_session()
    assert ps.get()[1] == "keychain"
    ps.delete_keychain()
    assert ps.get()[1] == "env"
    st = json.dumps(ps.status())
    assert "from-" not in st                       # 状態表示にパスワードは入らない


def test_keychain_unavailable(kr):
    from swing.passwords import PasswordStore
    kr(available=False)
    ps = PasswordStore()
    with pytest.raises(RuntimeError, match="キーチェーン"):
        ps.save_keychain("x")
    st = ps.status()
    assert not st["keychain_available"] and "環境変数" in st["keychain_note"]
    ps.use_for_session("x")                        # ① は使える
    assert ps.get() == ("x", "session")


def _fake_moomoo(good_pw=SECRET):
    m = types.ModuleType("moomoo")
    m.RET_OK = 0
    m.TrdMarket = types.SimpleNamespace(US="US")
    m.SecurityFirm = types.SimpleNamespace(FUTUJP="FUTUJP")

    class Ctx:
        calls = []

        def __init__(self, **kw):
            pass

        def unlock_trade(self, password=None, password_md5=None, is_unlock=True):
            Ctx.calls.append(("unlock" if is_unlock else "lock"))
            if password != good_pw:
                return -1, "unlock failed"
            return 0, None

        def __getattr__(self, name):
            if name in ("place_order", "modify_order"):
                raise AssertionError("注文してはいけない")
            raise AttributeError(name)

        def close(self):
            pass
    m.OpenSecTradeContext = Ctx
    return m


def test_verify_unlocks_then_relocks(monkeypatch):
    from swing import passwords
    from swing_helpers import cfg
    m = _fake_moomoo()
    monkeypatch.setitem(sys.modules, "moomoo", m)
    ok, msg = passwords.verify(cfg(), SECRET)
    assert ok and m.OpenSecTradeContext.calls == ["unlock", "lock"]
    ok, msg = passwords.verify(cfg(), "wrong")
    assert not ok and "違う" in msg and SECRET not in msg


def test_gui_password_flow_never_leaks(kr, monkeypatch, tmp_path):
    from swing import config as config_mod
    from swing import gui as g
    kr()
    monkeypatch.setitem(sys.modules, "moomoo", _fake_moomoo())
    raw = yaml.safe_load(open(config_mod.DEFAULT_PATH, encoding="utf-8"))
    raw["data"]["dir"] = str(tmp_path / "data")
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    app = g.App(str(p))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), g.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Token": app.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode()
    try:
        out = call("/api/password", {"action": "keychain", "password": SECRET})
        assert '"ok": true' in out and SECRET not in out
        assert json.loads(call("/api/status"))["password"]["source"] == "keychain"
        assert '"ok": true' in call("/api/job", {"name": "verify_password"})
        for _ in range(100):
            st = json.loads(call("/api/status"))
            if st["job"]["state"] != "running":
                break
            time.sleep(0.05)
        assert st["job"]["state"] == "done" and "正しい" in st["job"]["result"]["message"]
        assert SECRET not in json.dumps(st)                                  # 状態にもログにも出ない
        out = call("/api/password", {"action": "session", "password": "other"})
        assert json.loads(out)["password"]["source"] == "session"
        assert not list((tmp_path / "data").glob("**/*")) or all(
            SECRET not in f.read_text(errors="ignore") for f in (tmp_path / "data").glob("**/*") if f.is_file())
    finally:
        httpd.shutdown()
