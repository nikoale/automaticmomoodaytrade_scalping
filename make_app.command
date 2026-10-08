#!/bin/bash
# ===== Mac アプリを作る (初回と、新しい版をダウンロードしたとき) =====
# Finder でダブルクリック → 「アプリケーション」フォルダ (ホームの中) に「米国株スイング bot」ができます。
# 開けない場合: ターミナルで「bash 」と打ってから、このファイルをドラッグ＆ドロップして Enter。
cd "$(dirname "$0")" || exit 1
echo "=== 米国株スイング bot: Mac アプリを作ります ==="
VENV="$HOME/moomoo-swing/venv"

PY=""
for c in python3.12 python3.11 python3.13 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ ! -x "$VENV/bin/python" ]; then
  if [ -z "$PY" ]; then
    echo "❌ Python 3.11 以上が見つかりません。"
    echo "   https://www.python.org/downloads/macos/ から Python 3.12 をインストールして、もう一度開いてください。"
    open "https://www.python.org/downloads/macos/"
    read -r -p "Enter キーで閉じます"; exit 1
  fi
  echo "準備中です (初回は数分かかります)... 使用: $PY"
  mkdir -p "$HOME/moomoo-swing"
  "$PY" -m venv "$VENV" || { read -r -p "venv の作成に失敗しました。Enter で閉じます"; exit 1; }
fi
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
if ! "$VENV/bin/python" -m pip install -r requirements.txt; then
  read -r -p "❌ ライブラリのインストールに失敗しました。上のエラーを確認してください。Enter で閉じます"; exit 1
fi
APP=$("$VENV/bin/python" -m swing macapp | tail -1) || { read -r -p "❌ アプリを作れませんでした。Enter で閉じます"; exit 1; }
echo ""
echo "✅ できました: $APP"
echo "   次からは Finder の「アプリケーション」(ホームの中) または Launchpad から「米国株スイング bot」を開いてください。"
echo "   Dock に置くと便利です (開いている間に Dock のアイコンを右クリック →「オプション」→「Dock に追加」)。"
open "$APP"
read -r -p "Enter キーでこのウィンドウを閉じます"
