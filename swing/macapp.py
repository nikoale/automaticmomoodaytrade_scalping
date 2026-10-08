"""Mac アプリ (.app) を作る: python -m swing macapp (make_app.command から呼ぶ)。

作るもの: ~/Applications/米国株スイング bot.app
  Contents/Info.plist
  Contents/MacOS/launcher         ~/moomoo-swing/venv の Python で「python -m swing app」を起動する
  Contents/Resources/app/         プログラム一式 (swing/・config.yaml・requirements.txt) のコピー
  Contents/Resources/AppIcon.icns アイコン (Mac の sips / iconutil で作る。なければアイコンなし)

データ (~/moomoo-swing/data) はアプリの外なので、アプリを作り直しても消えない。
新しい版をダウンロードしたら make_app.command をもう一度開けば、アプリが新しい版に置き換わる。
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import ROOT

APP_NAME = "米国株スイング bot"
BUNDLE_ID = "local.moomoo-swing.bot"
VENV = Path("~/moomoo-swing/venv").expanduser()

LAUNCHER = """#!/bin/bash
# 米国株スイング bot (make_app.command が作ったアプリ)
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
PY="$HOME/moomoo-swing/venv/bin/python"
LOG="$HOME/moomoo-swing/logs"
mkdir -p "$LOG"
if [ ! -x "$PY" ]; then
  osascript -e 'display alert "米国株スイング bot" message "準備ができていません。ダウンロードしたフォルダの make_app.command をもう一度開いてください。"'
  exit 1
fi
cd "$RES/app" || exit 1
export SWING_APP_ICON="$RES/AppIcon.icns"
exec "$PY" -m swing app >> "$LOG/app.log" 2>&1
"""


def info_plist(version: str = "1.0") -> bytes:
    return plistlib.dumps({
        "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME, "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "launcher", "CFBundlePackageType": "APPL", "CFBundleIconFile": "AppIcon",
        "CFBundleShortVersionString": version, "CFBundleVersion": version,
        "LSMinimumSystemVersion": "11.0", "NSHighResolutionCapable": True,
    })


def draw_icon(png: Path, size: int = 1024) -> None:
    """アイコンの元画像: 紺の角丸の上に、右肩上がりの線と 20 日高値の点線。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    fig = plt.figure(figsize=(size / 100, size / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1), ax.set_ylim(0, 1), ax.axis("off")
    fig.patch.set_alpha(0)
    ax.add_patch(FancyBboxPatch((0.08, 0.08), 0.84, 0.84, boxstyle="round,pad=0,rounding_size=0.19",
                                fc="#10213d", ec="none"))
    xs = [0.2, 0.32, 0.42, 0.52, 0.6, 0.7, 0.8]
    ys = [0.32, 0.42, 0.37, 0.5, 0.47, 0.6, 0.74]
    ax.plot([0.2, 0.8], [0.56, 0.56], color="#7d8fb3", lw=size / 110, ls=(0, (1.2, 1.2)), solid_capstyle="round")
    ax.plot(xs, ys, color="#19c37d", lw=size / 45, solid_capstyle="round", solid_joinstyle="round")
    ax.scatter([xs[-1]], [ys[-1]], s=(size / 14) ** 2, color="#19c37d", zorder=3)
    fig.savefig(png, transparent=True)
    plt.close(fig)


def make_icns(dest: Path) -> bool:
    """Mac の sips / iconutil でアイコンを作る。ほかの OS や失敗時は False (アイコンなしで続ける)。"""
    if not (shutil.which("sips") and shutil.which("iconutil")):
        return False
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "icon.png"
        draw_icon(src)
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        for s in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = s * scale
                name = f"icon_{s}x{s}{'@2x' if scale == 2 else ''}.png"
                subprocess.run(["sips", "-z", str(px), str(px), str(src), "--out", str(iconset / name)],
                               check=True, capture_output=True)
        r = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest)], capture_output=True)
        return r.returncode == 0


def build(dest_dir: Path | None = None, icon: bool = True) -> Path:
    dest_dir = Path(dest_dir or Path("~/Applications").expanduser())
    dest_dir.mkdir(parents=True, exist_ok=True)
    app = dest_dir / f"{APP_NAME}.app"
    tmp = dest_dir / f".{APP_NAME}.building.app"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "Contents" / "MacOS").mkdir(parents=True)
    res = tmp / "Contents" / "Resources"
    res.mkdir()
    (tmp / "Contents" / "Info.plist").write_bytes(info_plist())
    launcher = tmp / "Contents" / "MacOS" / "launcher"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)
    appdir = res / "app"
    appdir.mkdir()
    shutil.copytree(ROOT / "swing", appdir / "swing", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in ("config.yaml", "requirements.txt"):
        shutil.copy2(ROOT / f, appdir / f)
    if icon:
        make_icns(res / "AppIcon.icns")
    if app.exists():
        shutil.rmtree(app)
    os.replace(tmp, app)
    return app
