# moomoo証券 米国株スキャルピング / デイトレード自動売買 bot

moomoo証券の **OpenAPI (OpenD 経由)** を使って、**米国株**を 1 分足ベースでスキャルピング・デイトレードする Python プログラムです。
moomoo証券(日本) の口座で、米国株の相場権限 **LV3** がある前提です。

- 3 つの戦略を同梱（画面 / 設定ファイルで切り替え）
- 損切り・利確・トレーリング・時間切れ・**引け前の強制決済** (持ち越しなし)
- 1 日の最大損失、連敗、取引回数などの **キルスイッチ**
- **銘柄の自動選定**（よく動いて売買の多い銘柄を開始時に選ぶ）
- 通常取引に加えて **時間外 (プレ / アフター / オーバーナイト)** にも対応
- バックテスト / paper (模擬約定) / simulate (moomoo 模擬口座) / live (実口座) の 4 モード
  — すべて同じ売買ロジックを使います
- **ブラウザで操作する GUI**（Mac は `start.command` をダブルクリック）

> ⚠️ **免責**: 本プログラムは学習・研究用のサンプルです。利益を保証するものではなく、
> 実際の売買で生じた損失について作者は一切責任を負いません。スキャルピングは手数料・スリッページ・
> 約定遅延の影響を強く受けます。**必ず backtest → paper → simulate で十分に検証し、少額から**始めてください。

---

## かんたん起動（GUI・Mac）

ターミナルに慣れていなくても、画面で操作できます。

1. moomoo の **OpenD** を起動してログインしておく
2. このフォルダの **`start.command` をダブルクリック**
   - 初回は自動で準備します（数分）。
   - 「マルウェアがないことを検証できません」と出たら、**システム設定 → プライバシーとセキュリティ** の
     「このまま開く」を押すか、ターミナルで `bash ` と打ってから `start.command` をドラッグ＆ドロップして Enter
   - Python 3.10〜3.12 が入っていない場合は案内ページが開きます
3. ブラウザに操作画面が開きます
   - 左の「設定」で 取引時間・銘柄・戦略・資金を選ぶ
   - **① 接続チェック** → **② 開始**
   - ダッシュボードで価格・ポジション（建値/損切り/利確ライン）・損益・約定履歴をリアルタイム表示
   - 「新規エントリー停止」「全ポジション決済」「停止」ボタンで操作
   - 「バックテスト」タブで、同じ設定のまま過去データ検証

**銘柄の自動選定（初期設定でオン）**: 候補（流動性の高い米国株・ETF 約60）から
「当日値幅 × 売買代金 × 出来高比率」の点数が高い銘柄を選びます。株価・売買代金・値幅・スプレッドの条件を
満たさない銘柄は除外。「候補ランキングを見る」で中身を確認できます。

起動しっぱなしでも、**30 分ごと**（変更可）と **時間帯の切り替わり（寄り付きなど）の 5 分後** に自動で選び直し、
銘柄を入れ替えます。ポジションを持っている銘柄は決済されるまで外さず、今の銘柄が上位（選ぶ数 × 2 以内）に
残っていれば入れ替えません。市場が閉まっている間は選び直しません。条件や候補は設定ファイルの `auto_symbols:` で変更できます。

GUI は自分の PC (127.0.0.1) からしか開けません。モードは 練習 (paper) / 模擬口座 / 実口座。
動作中は Mac の自動スリープを止めます（ふたを閉じると止まりますが、保有中の銘柄は口座側の逆指値で守られます）。
ターミナルのウィンドウを閉じるとボットも止まります。

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
| `scalper/market.py` | 呼値、取引時間帯 (日付またぎ対応)・引け前判定 |
| `scalper/screener.py` | 銘柄の自動選定 |
| `scalper/moomoo_broker.py` | moomoo への発注 (指値 → タイムアウトで取消) |
| `scalper/live.py` | OpenD からのリアルタイム受信と売買ループ、paper 約定 |
| `scalper/backtest.py` | CSV でのバックテストと成績集計 |
| `scalper/gui/` | ブラウザ GUI |

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
- 引けの `flatten_before_close_minutes` 分前に全決済
- ライブでは現在値プッシュごとに損切り/利確を判定（足の確定を待たない）

---

## セットアップ（コマンドで使う場合）

1. moomoo証券の口座を開設し、[moomoo OpenAPI のページ](https://openapi.moomoo.com/) から **OpenD** を
   ダウンロードして moomoo ID でログイン (既定で `127.0.0.1:11111` で待ち受け)
2. Python 3.10〜3.12

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/config.us.example.yaml config/config.us.yaml
```

## 使い方（コマンド）

### ⓪ 接続確認（市場時間外でも OK）

```bash
python -m scalper check -c config/config.us.yaml
```

OpenD へのログイン状態、相場権限、現在値・気配、直近の足、板を表示します。売買はしません。

### ① バックテスト（OpenD 不要）

```bash
# 動作確認用の擬似データ (※ランダムな偽データ。成績に意味はありません)
python -m scalper sample --days 30 --out data/sample_1m.csv
python -m scalper backtest -c config/config.us.yaml --csv data/sample_1m.csv
python -m scalper backtest -c config/config.us.yaml --csv data/sample_1m.csv --strategy orb --trades-out logs/bt.csv
```

本物の過去データは OpenD 起動中に取得できます:

```bash
python -m scalper fetch -c config/config.us.yaml --symbol US.NVDA --start 2026-09-01 --end 2026-10-01 --out data/nvda_1m.csv
python -m scalper backtest -c config/config.us.yaml --csv US.NVDA=data/nvda_1m.csv
# 複数銘柄:  --csv US.NVDA=data/nvda.csv US.AMD=data/amd.csv
```

### ② paper モード（リアル相場 + 模擬約定。お金は動きません）

```bash
python -m scalper run -c config/config.us.yaml --mode paper
```

moomoo の実際の最良気配で約定したと仮定してローカルで損益を計算します。
約定履歴は `logs/trades_paper_YYYYMMDD.csv` に保存されます。

### ③ simulate モード（moomoo の模擬口座）

```bash
python -m scalper run -c config/config.us.yaml --mode simulate
```

### 時間外取引（プレ / アフター / オーバーナイト）

```bash
cp config/config.us.ext.example.yaml config/config.us.ext.yaml
python -m scalper run -c config/config.us.ext.yaml --mode paper
```

`session.us_session` を `ETH`（プレ+アフター）/ `ALL`（+オーバーナイト）/ `OVERNIGHT` にすると時間外の足も受信します。
オーバーナイト (20:00〜翌4:00 ET) は**日本時間の昼間**にあたるので、日中に動作確認できます。
日付をまたぐ時間帯は `["20:00", "04:00"]` のように書き、`day_rollover: "20:00"` で
VWAP・1 日の損失上限が深夜 0 時でリセットされないようにします。
時間外は出来高が少なくスプレッドが広いので、まずは paper で。simulate / live での時間外注文は未検証です。

### ④ live モード（実口座。自己責任）

```bash
export MOOMOO_TRADE_PASSWORD='取引パスワード'     # 設定ファイルには書かない
python -m scalper run -c config/config.us.yaml --mode live --confirm-live
```

`Ctrl+C` で停止すると、ボットが建てたポジションは決済してから終了します。

---

## 実口座で使う

1. まず **練習（paper）** で数日、次に **模擬口座** で数日動かして、約定・決済・照合の流れを確認
2. GUI のモードで **実口座** を選び、取引パスワードを入力（保存されません）、
   「実際のお金で取引することを理解しました」にチェック
3. **① 接続チェック** で口座の総資産・買付余力・保有株を確認 → **② 実口座で開始**（確認ダイアログあり）
4. 最初は「1日の損失上限」「1ポジション上限」を小さくして始めてください

### 手数料に注意

moomoo証券の米国株手数料（ベーシック）は **約定代金の 0.132%（上限 22 USD）**、往復で 0.264% です。
1 分足スキャルピングの値幅はこれより小さいことが多く、**手数料負けしやすい**ため、

- 既定の足の長さを **5 分** にしています（GUI で 1/3/5/15 分を選べます）
- 利確幅が「往復の手数料 + スリッページ」の **2 倍** に満たない場面ではエントリーしません（`exits.min_reward_cost_ratio`）
- バックテスト・練習モードの損益も手数料込みで計算します

料金は変わることがあるので、最新の手数料は moomoo の公式サイトで確認し、違えば `execution.commission_rate` を変更してください。

## 安全装置

- **保護ストップ**: 建玉と同時に、口座側にも逆指値（GTC）を置きます。Mac のスリープ・回線切れ・ボット停止でも
  口座側で損切りされます。トレーリングで損切りが上がれば逆指値も動かし、ボットが決済するときは先に取り消します
- **口座との照合**: 30 秒ごとに口座の保有株数とボットの認識を照合。保護ストップの約定やアプリでの手動決済は
  自動で記録に反映。説明のつかないズレは、その銘柄の売買を止めて当日停止
- **残った注文の整理**: 起動時、前回ボットが出して残った注文を取り消し（保有株がある銘柄の保護ストップは残す）
- **買付余力**: 注文前に買付余力を確認し、足りなければ株数を減らす
- **暴走防止**: 新規注文が 1 分間に 10 回を超えたら当日停止（`execution.max_orders_per_minute`）
- 実口座は GUI で毎回パスワード入力・同意チェック・確認ダイアログが必要（パスワードはファイルにもログにも残りません）。
  コマンドの場合は `--confirm-live` が必要
- 起動時に口座に **既存ポジションがある銘柄は売買しません**（手動ポジションの誤決済防止）
- `max_daily_loss` に達すると当日は新規停止 & 保有ポジションを決済
- `max_consecutive_losses` 連敗で当日停止、負けトレード後は `cooldown_bars_after_loss` 本休む
- 1 トレードの損失が `account_size × risk_per_trade` に収まるよう株数を自動計算
- ATR が呼値 × `min_atr_ticks` 未満の値動きの小さい場面では見送り（スプレッド負けを防ぐ）
- 発注は「最良気配 ± `limit_offset_ticks` の指値」。`order_timeout_sec` 秒で約定しなければ取消
- 決済が通らなかった場合は 10 秒間隔で再試行（発注の連打を防ぐ）

## 注意点・制限

- **米国株専用**です（`trd_market: US` 以外や `US.` で始まらない銘柄コードはエラーになります）。
- **取引時間**: 通常取引は 9:30〜16:00 ET = 日本時間 22:30〜翌5:00（米国の冬時間は +1 時間）。
- **バックテストの約定**: シグナル足の終値 ± `slippage_ticks` で約定、損切りは足の安値/高値で判定し、
  同じ足で損切りと利確の両方に触れた場合は損切り優先（保守的）。実際の約定はこれより悪化し得ます。
- **空売り**: `allow_short: true` で有効（信用口座が必要）。既定は買いのみ。
- **API 制限**: moomoo OpenAPI には発注回数などの頻度制限があります。銘柄数や取引頻度を増やす場合は
  公式ドキュメントの制限を確認してください。

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
