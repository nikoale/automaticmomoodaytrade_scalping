#!/bin/bash
# ===== 米国株スイング bot を起動 (Mac) =====
# Finder でダブルクリック → 初回は自動で準備してから、ブラウザに操作画面が開きます。
# 開けない場合: ターミナルで「bash 」と打ってから、このファイルをドラッグ＆ドロップして Enter。
cd "$(dirname "$0")" || exit 1
echo "=== 米国株スイング bot ==="

if [ ! -x ".venv/bin/python" ] || ! .venv/bin/python -c "import pandas, yfinance, matplotlib" 2>/dev/null; then
  PY=""
  for c in python3.12 python3.11 python3.13 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo ""
    echo "❌ Python 3.11 以上が見つかりません。"
    echo "   https://www.python.org/downloads/macos/ から Python 3.12 をインストールして、もう一度開いてください。"
    open "https://www.python.org/downloads/macos/"
    read -r -p "Enter キーで閉じます"
    exit 1
  fi
  echo "初回セットアップ中です (数分かかります)... 使用: $PY"
  rm -rf .venv
  "$PY" -m venv .venv || { read -r -p "venv の作成に失敗しました。Enter で閉じます"; exit 1; }
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  if ! .venv/bin/python -m pip install -r requirements.txt; then
    rm -rf .venv
    read -r -p "❌ ライブラリのインストールに失敗しました。上のエラーを確認してください。Enter で閉じます"
    exit 1
  fi
fi

echo "画面を開きます。ブラウザが開かない場合は表示された URL を開いてください。"
echo "このウィンドウを閉じると止まります。"
# データ取得やバックテストの途中で Mac がスリープしないようにする
caffeinate -i .venv/bin/python -m swing gui
read -r -p "終了しました。Enter キーで閉じます"
