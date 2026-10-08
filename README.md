# moomoo証券 スキャルピング / デイトレード自動売買 bot

moomoo証券の **OpenAPI (OpenD 経由)** を使って、1 分足ベースのスキャルピング・デイトレードを自動で行う Python プログラムです。

- 日本株 (`JP.7203` など) と米国株 (`US.AAPL` など) に対応
- 3 つの戦略を同梱（設定ファイルで切り替え）
- 損切り・利確・トレーリング・時間切れ・**引け前の強制決済** (持ち越しなし)
- 1 日の最大損失、連敗、取引回数などの **キルスイッチ**
- バックテスト / paper (模擬約定) / simulate (moomoo 模擬口座) / live (実口座) の 4 モード
  — すべて同じ売買ロジックを使います

> ⚠️ **免責**: 本プログラムは学習・研究用のサンプルです。利益を保証するものではなく、
> 実際の売買で生じた損失について作者は一切責任を負いません。スキャルピングは手数料・スリッページ・
> 約定遅延の影響を強く受けます。**必ず backtest → paper → (米国株なら simulate) で十分に検証し、
> 少額から**始めてください。

---

## 仕組み

```
moomoo OpenD ──(K線/現在値/板 プッシュ)──▶ LiveRunner ──▶ SymbolEngine ──▶ Broker
                                                     │   ├ Strategy (いつ入るか)
                                                     │   ├ RiskManager (サイズ・キルスイッチ)
                                                     │   └ TradingSessions (取引時間)
CSV ─────────────────────────────────────────▶ backtest ┘
```

| ファイル | 役割 |
|---|---|
| `scalper/strategies.py` | 売買シグナル (3 戦略) |
| `scalper/engine.py` | エントリー・決済の判断 (バックテストとライブで共通) |
| `scalper/risk.py` | ポジションサイズ、1 日の損失上限、連敗停止、クールダウン |
| `scalper/market.py` | 東証の呼値テーブル、前場/後場・引け前判定 |
| `scalper/moomoo_broker.py` | moomoo への発注 (指値 → タイムアウトで取消) |
| `scalper/live.py` | OpenD からのリアルタイム受信と売買ループ、paper 約定 |
| `scalper/backtest.py` | CSV でのバックテストと成績集計 |

### 同梱戦略

| 名前 | タイプ | 概要 |
|---|---|---|
| `ema_vwap` | 順張り | 短期 EMA が長期 EMA を上抜け & 価格が VWAP の上 & 出来高増 & RSI 過熱でない → 買い |
| `orb` | 順張り (寄り付き) | 寄り後 N 分の高値/安値 (オープニングレンジ) をブレイクで追随。損切りはレンジ反対側 |
| `vwap_reversion` | 逆張り | VWAP から ATR×k 以上乖離 & RSI 極端 → VWAP へ戻る方向に。利確目標は VWAP |

### 決済ルール (全戦略共通、`exits:` で設定)

- 損切り: ATR × `stop_atr` / 利確: ATR × `target_atr`
- 建値ストップ: 含み益が ATR × `breakeven_atr` を超えたら損切りを建値 +1 ティックへ
- トレーリング: 最有利価格から ATR × `trail_atr`
- 時間切れ: `max_hold_bars` 本で決済
- 前場引け・大引けの `flatten_before_close_minutes` 分前に全決済
- ライブでは現在値プッシュごとに損切り/利確を判定（足の確定を待たない）

---

## セットアップ

### 1. moomoo 側の準備

1. moomoo証券の口座を開設
2. [moomoo OpenAPI のページ](https://openapi.moomoo.com/) から **OpenD** をダウンロードし、moomoo ID でログイン
   (OpenD は既定で `127.0.0.1:11111` で待ち受けます)
3. 初回は OpenAPI の利用規約への同意等が求められる場合があります。リアルタイム相場の取得には
   対象市場の相場権限が必要な場合があります（moomoo アプリ / 公式ドキュメントで確認してください）

### 2. Python 環境

Python 3.10 以上 (moomoo-api の対応バージョンに合わせて 3.10〜3.12 推奨)。

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
```

---

## かんたん起動（GUI・Mac）

ターミナルに慣れていなくても、画面で操作できます。

1. moomoo の **OpenD** を起動してログインしておく
2. このフォルダの **`start.command` をダブルクリック**
   - 初回は自動で準備します（数分）。「開発元を確認できません」と出たら、
     `start.command` を **右クリック → 開く** を選んでください
   - Python 3.10〜3.12 が入っていない場合は案内ページが開きます
3. ブラウザに操作画面が開きます
   - 左の「設定」で 市場・銘柄・戦略・資金を選ぶ
   - **① 接続チェック** → **② 開始**
   - ダッシュボードで価格・ポジション（建値/損切り/利確ライン）・損益・約定履歴をリアルタイム表示
   - 「新規エントリー停止」「全ポジション決済」「停止」ボタンで操作
   - 「バックテスト」タブで、同じ設定のまま過去データ検証

GUI は自分の PC (127.0.0.1) からしか開けません。安全のため実口座 (live) は GUI からは使えず、
paper と moomoo 模擬口座（米国株）のみです。ターミナルのウィンドウを閉じるとボットも止まります。

---

## 使い方（コマンド）

### ⓪ 接続確認（市場時間外でも OK）

```bash
python -m scalper check -c config/config.us.yaml
```

OpenD へのログイン状態、相場権限、現在値・気配、直近の足、板を表示します。売買はしません。

### ① バックテスト（OpenD 不要）

```bash
# 動作確認用の擬似データを生成 (※ランダムな偽データ。成績に意味はありません)
python -m scalper sample --days 30 --out data/sample_1m.csv
python -m scalper backtest -c config/config.yaml --csv data/sample_1m.csv
python -m scalper backtest -c config/config.yaml --csv data/sample_1m.csv --strategy orb --trades-out logs/bt.csv
```

本物の過去データは OpenD 起動中に取得できます:

```bash
python -m scalper fetch -c config/config.yaml --symbol JP.7203 --start 2026-09-01 --end 2026-10-01 --out data/7203_1m.csv
python -m scalper backtest -c config/config.yaml --csv JP.7203=data/7203_1m.csv
# 複数銘柄:  --csv JP.7203=data/7203.csv JP.9984=data/9984.csv
```

### ② paper モード（リアル相場 + 模擬約定。お金は動きません）

```bash
python -m scalper run -c config/config.yaml --mode paper
```

moomoo の実際の最良気配で約定したと仮定してローカルで損益を計算します。
**日本株は moomoo OpenAPI に模擬口座 (SIMULATE) がない**ため、日本株の練習はこのモードで行います。
約定履歴は `logs/trades_paper_YYYYMMDD.csv` に保存されます。

### ③ simulate モード（moomoo の模擬口座。米国株のみ）

```bash
cp config/config.us.example.yaml config/config.us.yaml
python -m scalper run -c config/config.us.yaml --mode simulate
```

### 米国株の時間外取引（プレ / アフター / オーバーナイト）

```bash
cp config/config.us.ext.example.yaml config/config.us.ext.yaml
python -m scalper run -c config/config.us.ext.yaml --mode paper
```

`session.us_session` を `ETH`（プレ+アフター）/ `ALL`（+オーバーナイト）/ `OVERNIGHT` にすると時間外の足も受信します。
オーバーナイト (20:00〜翌4:00 ET) は**日本時間の昼間**にあたるので、日中に動作確認できます。
日付をまたぐセッションは `["20:00", "04:00"]` のように書き、`day_rollover: "20:00"` で
VWAP・1 日の損失上限が深夜 0 時でリセットされないようにします。
時間外は出来高が少なくスプレッドが広いので、まずは paper で。simulate / live での時間外注文は未検証です。

### 暗号資産（24 時間・paper のみ）

```bash
cp config/config.crypto.example.yaml config/config.crypto.yaml
python -m scalper symbols -c config/config.crypto.yaml --market CC --grep BTC   # コードを確認して symbols に設定
python -m scalper check -c config/config.crypto.yaml
python -m scalper run -c config/config.crypto.yaml
```

数量は `lot_size: 0.0001` のような小数単位で計算します。板が取れない権限 (LV1 など) の場合は現在値＋スリッページで約定計算します。
moomoo OpenAPI に暗号資産の模擬口座がないため、simulate / live には対応していません。

### ④ live モード（実口座。自己責任）

```bash
export MOOMOO_TRADE_PASSWORD='取引パスワード'     # 設定ファイルには書かない
python -m scalper run -c config/config.yaml --mode live --confirm-live
```

`Ctrl+C` で停止すると、ボットが建てたポジションは決済してから終了します。

---

## 安全装置

- `--confirm-live` を付けないと実口座では起動しません
- 起動時に口座に **既存ポジションがある銘柄は売買しません**（手動ポジションの誤決済防止）
- `max_daily_loss` に達すると当日は新規停止 & 保有ポジションを決済
- `max_consecutive_losses` 連敗で当日停止、負けトレード後は `cooldown_bars_after_loss` 本休む
- 1 トレードの損失が `account_size × risk_per_trade` に収まるよう株数を自動計算（売買単位に丸め）
- ATR が呼値 × `min_atr_ticks` 未満の値動きの小さい場面では見送り（スプレッド負けを防ぐ）
- 発注は「最良気配 ± `limit_offset_ticks` の指値」。`order_timeout_sec` 秒で約定しなければ取消
- 決済が通らなかった場合は 10 秒間隔で再試行（発注の連打を防ぐ）

## 注意点・制限

- **呼値**: `tick_size: auto` は東証の標準テーブルです。トヨタなど **TOPIX500 構成銘柄** は
  `tick_size: topix500` にしてください。
- **バックテストの約定**: シグナル足の終値 ± `slippage_ticks` で約定、損切りは足の安値/高値で判定し、
  同じ足で損切りと利確の両方に触れた場合は損切り優先（保守的）。実際の約定はこれより悪化し得ます。
- **空売り**: 日本株の信用取引には未対応です（`allow_short` は米国株・バックテストのみ）。
- **API 制限**: moomoo OpenAPI には発注回数などの頻度制限があります。銘柄数や取引頻度を増やす場合は
  公式ドキュメントの制限を確認してください。
- 東証の取引時間は 2024/11/5 以降 9:00–11:30 / 12:30–15:30 です（`session:` で変更可）。

## 戦略を自作する

`scalper/strategies.py` の `Strategy` を継承して `on_bar()` で `Signal.LONG / SHORT / EXIT / NONE` を返し、
`STRATEGIES` に登録すれば `strategy.name` で選べます。損切り・利確価格を提案したい場合は
`self._stop_hint` / `self._target_hint` を設定します。

## テスト

```bash
pip install pytest
python -m pytest -q
```

moomoo SDK はフェイクに差し替えてテストしているので、OpenD なしで実行できます。
