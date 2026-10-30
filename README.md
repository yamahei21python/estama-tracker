# estama-tracker

大阪のメンズエステ（[estama.jp](https://estama.jp)）の出勤表を1時間おきに巡回し、「普段は完売しているのに、今日は空いているキャスト」と「このあと完売しそうなキャスト」を拾うための追跡システム。出勤枠の状態を SQLite に蓄積し、完売の履歴・速度・確率を分析して予約判断の材料を出す。

| ドキュメント | 内容 |
|---|---|
| [app/README.md](app/README.md) | 詳細仕様（コマンド・分析機能・データの注意点の全項目） |
| [app/RUNBOOK.md](app/RUNBOOK.md) | VM での1コマンド運用（セットアップ・cron・障害時） |
| [INTEGRATION_DESIGN.md](INTEGRATION_DESIGN.md) | 口コミ/爆サイ連携の設計メモ（帰属ルール・表示専用の境界） |

## 全体像

```
estama.jp 出勤表 ──(estama_scraper.py)──> estama.db: snapshots / slot_events
                                             │
                     (estama_tracker.py)─────┼──> report / alert / notify / dashboard
                                             │
爆サイ.com ──(bakusai.py)──> bakusai.db ─────┤
口コミDB (kagawa_menes.db) ─────────────────┘──> evidence.py（alert の根拠表示のみ）

runner.py = 巡回・取込・検証・日次アーカイブ・data ブランチへの publish
```

- **取り込み**: ウォッチリスト `app/shops.json`（74店）の当日分を巡回して `snapshots` に追記。`snapshots` は不変で、30分枠の状態変化だけを別テーブル `slot_events` にイベントとして記録する
- **分析**: 完売一覧・完売率・ストリーク・勢い（枠消化）・完売スピード・エリア/店舗傾向・ランキング照合
- **予測**: 時間帯別 pooled hazard ＋ キャスト自身の完売ベースラインから「4時間以内に完売する確率」
- **根拠**: alert に口コミ・爆サイ・枠消失時刻・出典URLを添える（表示専用。判定計算には使わない）
- **監視**: 取得失敗率・0行店・checkpoint 欠損・枠イベント取得率を日次レポート冒頭で警告
- **配信**: DB は `storage.json` の設定先（現在は GitHub `data` ブランチ、次ゴールで R2）へ **1実行1コミット** で publish。HTML ダッシュボードと push 通知も生成できる

## リポジトリ構成

| パス | 役割 |
|---|---|
| `runner.py` | 運用ランナー。ロック・DB検証・アーカイブ・data ブランチへのアップロード（blob SHA 照合／GitHub では失敗時に前版へ復元）・`run_log` |
| `app/estama_scraper.py` | 出勤表の巡回・`snapshots`/`slot_events` への取込・完売スピード分析 |
| `app/ranking_scraper.py` | 週間人気ランキング（14エリア×30人）取得・`cast_rankings` 保存・ウォッチリスト同期 |
| `app/estama_tracker.py` | 分析 CLI。report / alert / sellout-forecast / monitor / notify / dashboard / cast など |
| `app/bakusai.py` | 爆サイ.com のスレッド発見・本文取得（専用 DB `bakusai.db`、VMローカル） |
| `app/evidence.py` | alert の根拠表示（口コミ・爆サイ・枠消失・出典URL。表示専用） |
| `app/common.py` | 共通部品（パス、UA、セッション、DB接続、ステータス正規化、`parse_clock` など） |
| `app/schema.sql` | スキーマ（`shops` / `snapshots` / `cast_rankings` / `daily_cast_status` / `run_log` / `scrape_log` / `forecast_eval` / `slot_events` / `cast_watch`） |
| `app/shops.json` | 巡回対象のウォッチリスト |
| `app/deep_shops.json` | deep tier（爆サイでスレッドが確認できた店だけの固定リスト。口コミ順位で埋めない） |
| `tests/` | 標準ライブラリのみのテスト（15ファイル、ネットワーク不要） |

DB（`*.db`）と `storage.json`、`banned_shops.json`、`dashboard.html`、`logs/`、`staging/`、`r2_local/` はランタイム成果物のため git 管理外。`bakusai.db` は VM ローカルの別 DB で、data ブランチにも上げない。`.perch/mcp.json`（ローカルCLI設定・認証情報を含み得る）も管理外。

## クイックスタート（ローカル）

```bash
python3 -m venv venv
./venv/bin/pip install requests beautifulsoup4        # R2 を使う場合のみ boto3

# スキーマ適用（冪等。既存 DB に slot_events 等を追加するだけ。snapshots は不変）
./venv/bin/python app/estama_tracker.py verify --db app/estama.db

# 1店だけ取得して取り込み（既定: 40900 SUPER AKUMA GIRLS）
./venv/bin/python app/estama_scraper.py --shop-id 40900 --import-db

# 全店巡回（当日分のみ）
./venv/bin/python app/estama_scraper.py --all-shops --import-db

# 全セクション分析（冒頭に監視警告）
./venv/bin/python app/estama_tracker.py report --date 2026-10-30
```

- 保存先は `storage.json`（VM 側、リポジトリには含めない）で切り替える。`github`（`data` ブランチに gzip 保存）／R2／未設定なら `r2_local/` へのローカルミラー
- 爆サイを使う場合は専用 DB を初期化する: `./venv/bin/python app/bakusai.py init`

## 主要コマンド

| 目的 | コマンド |
|---|---|
| 巡回・取込 | `app/estama_scraper.py --all-shops --import-db` |
| ランキング＋ウォッチ同期 | `app/ranking_scraper.py --area all --import-db --sync-watchlist` |
| 完売一覧／件数 | `app/estama_tracker.py sold-out --date D` / `counts --date D` |
| アラート（根拠付き） | `app/estama_tracker.py alert --date D --min-days 3 --threshold 0.7` |
| 4時間以内の完売確率 | `app/estama_tracker.py sellout-forecast --deep-shops app/deep_shops.json` |
| 日次レポート | `app/estama_tracker.py report --date D`（`--no-evidence` で根拠省略） |
| 監視（警告時 exit 1） | `app/estama_tracker.py monitor --db app/estama.db`（既定日=現在の営業日。`--date` は `YYYY-MM-DD` 厳格） |
| 爆サイ | `app/bakusai.py discover` / `crawl --deep app/deep_shops.json` / `status` |
| 通知・ダッシュボード | `app/estama_tracker.py notify` / `dashboard --out dashboard.html` |
| 個別キャスト | `app/estama_tracker.py cast --shop-id X --cast-id Y` |

### 予測・根拠・監視の要点

- **slot_events**: 30分枠の状態変化（○→×/─/TEL、戻り含む）だけを1行。`slot_time` は表記と分（`27:00`=1620）を併記。初観測の `from_mark` は NULL。未来日のテンプレ枠は記録しない
- **sellout-forecast**: 営業時間帯ごとの pooled hazard × √(キャストの完売率)。`n>=3`・`p>=0.5`・watch/deep 対象・上位5件のみ確率を表示。履歴不足なら確率を出さずベースライン順位だけ出す
- **evidence**: 口コミ n件・スコア・タグ2つ（取得日付き）、爆サイ返信の最終投稿と引用、枠の消失時刻、出典URL。**表示専用**で、完全名一致＋店舗紐付け信頼度 0.8 以上のみ（伏せ字は候補保存だけで自動確定しない）
- **monitor**: `run_log` / `scrape_log` / `slot_events` を突き合わせ、checkpoint 未達・runner 滞留・店別 fail 率・0行店・枠イベント取得率を先頭に出す

## 運用（VM）

実行は Muse の VM、閲覧・分析は Mac。cron は以下（詳細は [app/RUNBOOK.md](app/RUNBOOK.md)）:

| 時刻 | コマンド |
|---|---|
| 毎時 :30 | `./venv/bin/python runner.py checkpoint` |
| 毎日 06:15 | `./venv/bin/python runner.py ranking` |
| 毎日 05:10 | `./venv/bin/python app/bakusai.py crawl --deep app/deep_shops.json` |
| 毎日 06:35 | `./venv/bin/python app/estama_tracker.py monitor` |

publish は1実行1コミット。書込み直後に blob SHA を照合し、不一致なら1回だけ再送、それでも不一致なら前の blob に戻す（前版が無ければキーを削除）。壊れた DB が最新版を潰さない。publish が失敗した実行は `run_log` を `fail` にして runner が exit 1 する（cron 側で気づける）。

日次アーカイブは「閉じた営業日」（期待営業日の前日）をキーに `archive/estama-<営業日>.db` を1回だけ保存する。サイトが最終チェックポイントより先に日付を切り替えても取り逃さず、アーカイブ失敗時は checkpoint/ranking を止めずに次回再試行する（検証不成立のキーは削除）。

## テスト

すべて標準ライブラリのみ・ネットワーク不要。

```bash
for t in tests/test_*.py; do ./venv/bin/python "$t" || break; done
```

| ファイル | 対象 |
|---|---|
| `test_slot_events.py` | 枠パース・イベント重複排除・シフト内外判定・遅延リプレイ拒否 |
| `test_hazard.py` | 時間帯別 hazard・ベースライン・確率の出し方 |
| `test_evidence.py` | 根拠表示（口コミ/爆サイ/枠消失/出典・失敗時も alert 不変） |
| `test_bakusai.py` | 発見・紐付け信頼度・検索フォールバック・crawl 再開/重複排除・404継続・リダイレクト課金 |
| `test_monitor.py` | 監視の警告条件 |
| `test_gh_storage.py` | data ブランチ保存の blob 検証・復元 |
| `test_runner.py` | ロック・検証・アーカイブ（閉じた営業日）・1コミット publish・publish 失敗の exit 1 |
| ほか（final_states / rollup / prune / speed_rank / timestamps / normalize / tracker_alert / gemini_retry） | 既存の分析・取込ロジック |

## データの注意（必読・抜粋）

- **意味があるのは当日分だけ**。未来日の行はサイト側テンプレートで、完売状態を表さない（既定 `--days 1`）
- **完売判定はステータス文言のみ**。枠の ○/×/─ は使わない（未来日のダミー対策）
- **R3 最終状態**は分析時にのみ適用する（終業時にサイトが `closed` へリセットするため、最後の non-closed 観測を最終状態とする）。DB 行は生のまま
- **集計キーは `cast_id`**（`COALESCE(cast_id, cast_name)`）。改名で1人が2人に分裂するのを防ぐ
- **シフト時刻の文字列比較は禁止**。`9:00` / `29:00` 表記があるため `common.parse_clock()` の分単位 int で比較する
- **slot_events はデプロイ後の巡回から埋まる**。初回は `runner.py checkpoint` を1回実行してから `monitor` を回す
- 詳細（判定規則・ダミー/おとり・TELのみ・巡回欠損など）は [app/README.md](app/README.md) を参照

## スコープと今後

- 現在の publish 先は GitHub `data` ブランチ（暫定）。**次ゴールで R2 移行**
- スコープ外: LLM 利用、口コミの再取得、過去分のバックフィル、外部への即時通知
- 爆サイ取得は 1.5〜2.0 秒間隔・429 は Retry-After 尊重・1日500リクエスト上限で、再開可能（カーソルは `bakusai_state`）
- VM への反映（clone / cron 設定 / 初回実行）は運用者が任意のタイミングで行う
