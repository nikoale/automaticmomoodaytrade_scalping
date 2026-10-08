# 米国株スイング自動売買ボット（moomoo OpenD）

週次スクリーナー → 日次エントリー判定 → 発注・リスク管理 → ログ の 4 部品で構成する、米国株のスイング自動売買ボット。
戦略は「20 日高値ブレイクのトレンドフォロー」。運用資金 20 万円・最大 3 銘柄・現金口座 (T+1) を前提にしている。

> ⚠ 学習・研究用です。利益を保証しません。実際の売買による損失について作者は責任を負いません。
> **本番口座 (TrdEnv.REAL) への発注は、ユーザーが明示的に許可するまで有効化しません。**

## 進捗（事実のみ）

| フェーズ | 部品 | 状態 |
|---|---|---|
| 1 | 週次スクリーナー `swing/screener.py` | **実装済み・テスト済み**（手作りデータ）。**実データでは未検証**（開発環境からデータ取得先に接続できなかったため） |
| 2 | 戦略 `swing/strategy.py` / バックテスト `swing/backtest.py` / レポート `swing/report.py` | **実装済み・テスト済み**（手作りデータ・擬似データ）。**実データでのバックテスト結果は未作成** |
| 2 | 資金管理 `swing/risk.py`（サイズ・手数料・T+1・週次損失上限） | バックテストで使う部分は実装済み・テスト済み |
| 3 | 発注 `executor.py`（SIMULATE、証券会社側の逆指値、二重ロック） | **未実装**（フェーズ 2 の結果を見てから着手） |
| 4 | 常駐 `runner.py`（日次処理・サマリー出力） | **未実装** |

要確認事項は [docs/要確認事項.md](docs/要確認事項.md)。

※ 以前のデイトレ版 (`scalper/`) はまだリポジトリに残っています（削除は保留中）。新しいコードはすべて `swing/` です。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `config.yaml` | すべての設定値（コードにハードコードしない） |
| `swing/data.py` | 銘柄リスト・日足・時価総額・決算日の取得とキャッシュ (`data/`) |
| `swing/indicators.py` | 移動平均・ATR・20 日高値など（日付 × 銘柄のパネルで一括計算） |
| `swing/screener.py` | 週次スクリーナー（ライブとバックテストで同じ関数 `screen_at` を使う） |
| `swing/strategy.py` | エントリー・損切り・トレーリング・手仕舞いのルール（純粋関数） |
| `swing/risk.py` | ポジションサイズ・手数料・T+1 受渡・週次損失上限 |
| `swing/backtest.py` | 口座シミュレーション型のバックテストと評価指標 |
| `swing/report.py` | レポート（Markdown・図・CSV） |
| `swing/calendar_us.py` | NYSE の営業日カレンダー、日本時間・夏時間の変換 |

## セットアップ

Python 3.11 以上。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # テスト (ネットワーク不要)
```

## データ

無料のデータ源を使います（すべて `data/` に CSV でキャッシュし、2 回目以降は差分だけ取得）。

| データ | 取得元（既定） | 代替 | 入手方法 |
|---|---|---|---|
| 銘柄リスト（米国上場の普通株） | Nasdaq Trader 銘柄ディレクトリ `nasdaqlisted.txt` / `otherlisted.txt` | — | 無料・キー不要。ETF・テスト銘柄・SPAC・ワラント・優先株・ADR を除外 |
| 日足 10 年以上 | Yahoo Finance（`yfinance`、分割・配当調整済み） | Stooq（CSV） | 無料・キー不要。`config.yaml` の `data.price_source` で切替 |
| 時価総額・発行済株数 | Yahoo Finance（`fast_info`） | moomoo `get_market_snapshot` の `total_market_val`（要確認） | 無料 |
| 決算日（過去・予定） | Yahoo Finance（`get_earnings_dates`） | moomoo `get_earnings_calendar`（要確認） | `data.earnings_source` で切替 |

```bash
python -m swing fetch all            # 初回: 銘柄リスト → 日足 → 時価総額・決算日（数十分〜）
python -m swing fetch all --limit 50 # 動作確認だけなら先頭 50 銘柄
python -m swing fetch prices         # 以降: 日足の差分更新
```

Yahoo Finance は非公式 API なので、仕様変更や一時的な取得制限があり得ます（失敗した銘柄はログに出して続行）。

## 週次スクリーナー（フェーズ 1）

```bash
python -m swing screen               # データ更新 → data/watchlists/watchlist_YYYYMMDD.json
python -m swing screen --no-update   # キャッシュのデータで実行
```

- 直近の米国取引日（日本時間 土曜の朝なら金曜）の引けまでのデータで判定
- 母集団: 普通株 / 株価 10〜100 ドル / 20 日平均出来高 50 万株以上 / 時価総額 3 億ドル以上 / 次回決算が 15 営業日以内なら除外
- トレンド: 終値 > 50 日線 > 200 日線
- ランク: ① 6 ヶ月（126 営業日）上昇率が**市場全体（全普通株）**の上位 20% → ② 出来高の伸び（20 日平均 / 100 日平均）の大きい順に上位 20
- SPY が 200 日線を下回る週は監視リストを空にして「新規停止」をログに出す
- 各銘柄に 20 日高値・ATR(14)・次回決算日を添付。決算日が取れない銘柄はログに「要確認」と出して除外（`earnings_unknown_policy`）

## バックテスト（フェーズ 2）

```bash
python -m swing backtest             # → reports/<日時>/report.md（図・CSV 付き）
python -m swing backtest --synthetic # 擬似データでレポート作成の流れだけ確認（成績に意味はない）
```

レポートに出すもの: 総損益・年率リターン・勝率・損益レシオ・最大ドローダウン・年別損益・取引回数・平均保有日数・
資金が遊んでいた割合（現金比率・ノーポジション日の割合）、損益曲線・ドローダウンの図、全トレードと判断ログの CSV。

実行する組み合わせ（各期間は資金 20 万円から独立に開始、パラメータ最適化なし）:
- 全期間（指数フィルター あり / なし）
- 検証期間 / 検証外期間（直近 2 年）
- 2020 年・2022 年（指数フィルター あり / なし）

シミュレーションのルール:
- 判定は日足の終値、執行は翌営業日の寄り付き（成行、スリッページ片道 0.1%）
- 損切りは証券会社側の逆指値の想定なので、日中に安値が触れたら約定（寄り付きで下回っていれば寄り付き値）
- 手数料 片道 0.132%（上限 22 ドル）、両替コストは config
- 現金口座: 売却代金は T+1 で受渡、受渡前の代金では買えない
- 最大 3 銘柄・ランク上位から・同じ銘柄は手仕舞いから 5 営業日は再エントリーしない・週次損失 6% で新規停止

**バックテストの限界**（[要確認事項](docs/要確認事項.md) の 6 章）: 生存者バイアス、過去の時価総額は近似、調整後価格。

## OpenD 接続（フェーズ 3 で使用・未実装）

- moomoo の OpenAPI ページから OpenD をダウンロードし、moomoo ID でログイン（既定 `127.0.0.1:11111`）
- VPS (Linux) ではコマンドライン版 OpenD を使い、設定ファイルにログイン情報を書いて常駐させる（具体的な設定項目は公式ドキュメントで要確認）
- `config.yaml` の `moomoo:` に接続先。`trd_env: SIMULATE` 固定

## SIMULATE → REAL 切替（フェーズ 3 で実装予定・未実装）

設計（予定）: `config.yaml` の `moomoo.allow_real: true` **かつ** 起動時の確認プロンプトで決められた文字列を入力した場合だけ
REAL で発注する二重ロック。どちらか一方だけでは SIMULATE のまま。**ユーザーの明示的な許可があるまで有効化しない。**

## VPS 常駐（フェーズ 4 で実装予定）

今動かせるのは週次スクリーナーだけです。日次処理 (`runner.py`) は未実装。

cron の例（VPS のタイムゾーンを Asia/Tokyo にしている場合）:

```cron
# 毎週土曜 9:00 (日本時間) に週次スクリーナー
0 9 * * 6  cd /opt/swing && .venv/bin/python -m swing screen >> logs/cron.log 2>&1
# 平日の翌朝 7:30 (日本時間) に日次処理  ※runner は未実装
# 30 7 * * 2-6  cd /opt/swing && .venv/bin/python -m swing daily >> logs/cron.log 2>&1
```

systemd timer の例:

```ini
# /etc/systemd/system/swing-screen.service
[Service]
Type=oneshot
WorkingDirectory=/opt/swing
ExecStart=/opt/swing/.venv/bin/python -m swing screen

# /etc/systemd/system/swing-screen.timer
[Timer]
OnCalendar=Sat *-*-* 09:00:00 Asia/Tokyo
Persistent=true
[Install]
WantedBy=timers.target
```

日次処理は「日本時間 7:30」に固定すると、米国の夏時間（引け 5:00 JST）・冬時間（6:00 JST）のどちらでも引け後になります。
実行時には `calendar_us.last_completed_session()` で「引けまで終わった直近の取引日」を判定するので、休場日や時刻のずれにも対応します。

## ログ

`logs/YYYYMMDD_<コマンド>.log` に日付別で出力。スクリーナーは各段階の残り銘柄数・決算日が取れない銘柄（要確認）・
選ばれた銘柄の数値を、バックテストは判断ログ（候補・エントリー・損切り更新・手仕舞い・見送り）を CSV にも出力します。
