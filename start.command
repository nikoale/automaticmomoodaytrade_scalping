#!/bin/bash
# ===== moomoo スキャルピング bot 起動 (Mac) =====
# Finder でこのファイルをダブルクリックすると、初回は自動で準備してから GUI を開きます。
cd "$(dirname "$0")" || exit 1

echo "=== moomoo スキャルピング bot ==="

if [ ! -x ".venv/bin/python" ]; then
  PY=""
  for c in python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo ""
    echo "❌ Python 3.10〜3.12 が見つかりません。"
    echo "   https://www.python.org/downloads/macos/ から Python 3.12 をインストールしてから、"
    echo "   もう一度このファイルをダブルクリックしてください。"
    open "https://www.python.org/downloads/macos/"
    read -r -p "Enter キーで閉じます"
    exit 1
  fi
  echo "初回セットアップ中です (数分かかります)... 使用: $PY"
  "$PY" -m venv .venv || { read -r -p "venv の作成に失敗しました。Enter で閉じます"; exit 1; }
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  if ! .venv/bin/python -m pip install -r requirements.txt; then
    rm -rf .venv
    read -r -p "❌ ライブラリのインストールに失敗しました。上のエラーを確認してください。Enter で閉じます"
    exit 1
  fi
fi

echo "GUI を起動します。ブラウザが開かない場合は表示された URL を開いてください。"
echo "このウィンドウを閉じると bot も停止します。"
.venv/bin/python -m scalper gui
read -r -p "終了しました。Enter キーで閉じます"
