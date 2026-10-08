"""Mac アプリ (.app) の組み立てと、画面を 2 つ起動しないこと。"""
import json
import os
import plistlib
import threading

from swing import macapp


def test_bundle_layout(tmp_path):
    app = macapp.build(tmp_path, icon=False)
    assert app.name == "米国株スイング bot.app"
    c = app / "Contents"
    info = plistlib.loads((c / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "launcher" and info["CFBundleIdentifier"] == macapp.BUNDLE_ID
    launcher = c / "MacOS" / "launcher"
    assert os.access(launcher, os.X_OK) and "-m swing app" in launcher.read_text()
    assert (c / "Resources/app/swing/gui.py").exists() and (c / "Resources/app/config.yaml").exists()
    assert not list((c / "Resources/app").rglob("__pycache__"))
    macapp.build(tmp_path, icon=False)                    # 作り直しても壊れない
    assert app.exists() and not list(tmp_path.glob(".*building*"))


def test_icon_png(tmp_path):
    p = tmp_path / "i.png"
    macapp.draw_icon(p, size=256)
    assert p.read_bytes()[:4] == b"\x89PNG"


def test_second_instance_reuses_first(tmp_path):
    import yaml

    from swing import config as config_mod
    from swing import gui as g
    raw = yaml.safe_load(open(config_mod.DEFAULT_PATH, encoding="utf-8"))
    raw["data"]["dir"] = str(tmp_path / "data")
    cp = tmp_path / "config.yaml"
    cp.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    assert g.running_instance(str(cp)) is None
    app, httpd, url = g.make_server(18765, str(cp))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert g.running_instance(str(cp)) == url
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert g.running_instance(str(cp)) is None              # 止まったサーバーの記録は無視
    (tmp_path / "data" / "gui_instance.json").write_text(json.dumps({"pid": 999999, "url": url}))
    assert g.running_instance(str(cp)) is None
