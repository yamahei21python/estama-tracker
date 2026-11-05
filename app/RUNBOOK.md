# VM 運用手順書（1コマンド運用）

estama-tracker を Muse の VM で動かすための手順。**VM への反映（clone / cron 設定 / 初回実行）は任意のタイミングでユーザーが行う**。ここでは「どのコマンドを、どの順で、何のために」だけをまとめる。

前提: リポジトリを `~/estama-tracker` に置き、`storage.json`（`{"backend": "github", ...}`）は既存のまま使う。DB は data ブランチ（または R2）が正で、VM の `app/estama.db` はワーキングコピー。

---

## 1. 初回セットアップ（1回だけ）

```bash
cd ~/estama-tracker
python3 -m venv venv
./venv/bin/pip install requests beautifulsoup4

# スキーマ適用（冪等。既存 DB に slot_events 等の追加テーブル/列を作るだけ。snapshots は不変）
./venv/bin/python app/estama_tracker.py verify --db app/estama.db

# 爆サイ専用 DB の作成（estama.db とは完全に別ファイル）
./venv/bin/python app/bakusai.py init
```

任意: 口コミ根拠を alert に出したい場合だけ、レビュー DB を VM に置く。

```bash
# 既定は ~/Myproject/menesureview/kagawa_menes.db。別の場所なら環境変数で指定
export ESTAMA_REVIEW_DB=/path/to/kagawa_menes.db
```

レビュー DB が無くても alert は動く（口コミ根拠の行が省略されるだけ）。

### 保存先の切り替え（任意・R2）

GitHub `data` ブランチは毎時DB全体を上書きするため履歴が単調増加する（約120MB/日）。R2 へ切り替える場合は環境変数を設定し、`storage.json` から `backend: github` を外す（保持設定を使うならファイルは残す）:

```bash
export ESTAMA_R2_ENDPOINT=https://...
export ESTAMA_R2_BUCKET=...
export ESTAMA_R2_ACCESS_KEY_ID=...
export ESTAMA_R2_SECRET_ACCESS_KEY=...
# storage.json: {"archive_retention": {"days": 30, "weekly": true}}
```

保持設定が未設定の間はアーカイブを削除しない。有効化すると publish 成功後に best-effort で走り、直近N日は日次、それ以前は月曜分だけ残す。

---

## 2. 定期ジョブ（既存 cron のまま）

| 時刻 | コマンド |
|---|---|
| 毎時 :30 | `./venv/bin/python runner.py checkpoint` |
| 毎日 06:15 | `./venv/bin/python runner.py ranking` |

data ブランチへの書き込みは **1 実行 1 コミット**。書き込み直後に blob SHA を照合し、不一致なら 1 回だけ再送、それでも不一致なら前の blob に自動で戻す（壊れた DB が最新版を潰さない）。

---

## 3. 爆サイ discovery / crawl（新規・別 DB）

```bash
# スレッド発見（7ボード最新30件 + 店名のエリア検索 + 表記ゆれ辞書）
# 初回と、deep tier を見直すときだけ。1日500リクエスト上限を crawl と共有
./venv/bin/python app/bakusai.py discover --deep-out app/deep_shops.json

# 日次取得（deep tier のスレッドだけ。バックフィルは再開可能）
./venv/bin/python app/bakusai.py crawl --deep app/deep_shops.json --estama-db app/estama.db

# 動作確認用の小規模 crawl（最新2スレッド × 1ページだけ。数リクエストで済む）
./venv/bin/python app/bakusai.py crawl --deep app/deep_shops.json --limit 2 --max-pages 1

# 状態確認（threads / posts / links / 当日リクエスト数）
./venv/bin/python app/bakusai.py status
```

- `app/deep_shops.json` は「スレッドが確認できた店」だけで確定した固定 JSON（口コミ上位で埋めない）。リポジトリに含めるので VM で `discover` は不要。見直すときだけ `discover` を再実行する（ローカルで確認済み: 40 店）。
- `bakusai.db` は VM ローカルの別 DB。data ブランチには上げない。
- crawl は 1.5〜2.0 秒間隔・429 は Retry-After 尊重・1日500リクエストで自動停止し、翌日同じコマンドで続きから再開する（カーソルは `bakusai_state`）。
- 検索が項目不足/Cookie 欠落で失敗した場合は「最新30件＋辞書」に自動フォールバックする（`status` の `mode` で確認できる）。

cron 例（日次 05:10、重いバックフィルは夜間に回す）:

```cron
10 5 * * * cd ~/estama-tracker && ./venv/bin/python app/bakusai.py crawl --deep app/deep_shops.json >> bakusai.log 2>&1
```

---

## 4. 日次レポート・監視・予測（新規）

```bash
# 監視レポート: 店別 fail 率・0行店・checkpoint 欠損・枠イベント取得率を冒頭に警告表示
# 警告があれば exit 1（cron の失敗通知にそのまま使える）
./venv/bin/python app/estama_tracker.py monitor --db app/estama.db

# 「4時間以内に完売する確率」: watch/deep 対象・n>=3・p>=0.5・上位5件
# 履歴不足のキャストは確率を出さずベースライン順位のみ表示
./venv/bin/python app/estama_tracker.py sellout-forecast --deep-shops app/deep_shops.json

# 全セクション分析（alert に口コミ/爆サイ/枠消失/出典 URL の根拠行が付く）
./venv/bin/python app/estama_tracker.py report --date 2026-10-29
```

cron 例（毎日 06:35、ranking の後）:

```cron
35 6 * * * cd ~/estama-tracker && ./venv/bin/python app/estama_tracker.py monitor >> monitor.log 2>&1
```

`notify` は runner が各 job 後に best-effort で呼ぶ既存のまま。外部即時通知の追加はこのゴールのスコープ外。

---

## 5. 動作確認（デプロイ後に1回）

```bash
# 既存テスト（すべて標準ライブラリ、ネットワーク不要）
for t in tests/test_*.py; do ./venv/bin/python "$t" || break; done

# DB の整合性と件数
./venv/bin/python app/estama_tracker.py verify --db app/estama.db

# 監視が exit 0 で終わること（warning が残っていないこと）
# 注意: slot_events はデプロイ後の巡回から記録される。初回は runner.py checkpoint を
# 1 回実行してから monitor を回す（それ以前は「枠イベント 0%」等で exit 1 が正常）
./venv/bin/python app/estama_tracker.py monitor --db app/estama.db; echo "exit=$?"

# 爆サイ DB の状態（threads > 0 / links > 0 を確認）
./venv/bin/python app/bakusai.py status
```

再実行しても重複しない: `slot_events` は (shop_id, cast_key, target_date, slot_time) の変化時のみ 1 行、`bakusai_posts` は (thread_id, post_no) 主キーで `INSERT OR IGNORE`、`bakusai_shop_map` は thread×shop の upsert。同じ discover / crawl / checkpoint を 2 回流しても件数は増えない。

---

## 6. トラブル時

| 症状 | 対処 |
|---|---|
| `monitor` が exit 1 | 冒頭の警告（checkpoint 欠損 / 0行店 / fail 率 / 枠イベント取得率）を確認。店別 fail なら `scrape_log`、scheduler 側なら `run_log` |
| 爆サイ search が failed | セッション Cookie が取れていない（最初の GET が失敗）。ネットワーク復旧後に `discover` を再実行。失敗中も最新30件＋辞書で動く |
| crawl が止まった | 1日500リクエスト上限。翌日同じコマンドで再開（バックフィルはページ単位で保存済み） |
| publish が失敗した | GitHub では runner が自動で前の blob に戻す（前版が無ければキー削除）。`run_log` は fail になり runner は exit 1 する。DB 自体は VM のワーキングコピーに残るので、次回 job の publish で再送される |
| 口コミ根拠が出ない | `ESTAMA_REVIEW_DB` のパスと DB の存在を確認（無くても他の根拠は出る） |

---

## 7. GitHub Actions 代走（ステートレス）

R2 が正なので、runner はローカル状態なしで動く。`.github/workflows/collect.yml` が
「GET → job → PUT」を行う。VM と違い、DB は毎回ダウンロードする。

前提（repo 設定）: secret `GATEWAY_TOKEN` と variable `GATEWAY_BASE`（Worker の URL）。
どちらも設定されていないと workflow は最初のステップで止まる。

### 試走（切り替え前）

```bash
gh workflow run collect.yml -f job=checkpoint -f mode=primary
gh run watch "$(gh run list --workflow=collect.yml --limit 1 --json databaseId -q '.[0].databaseId')"
```

受け入れ基準（VM 実行と比較）:

| 項目 | 見る場所 | 合格 |
|---|---|---|
| 取得 | `scrape_log` の ok/fail 率 | VM と同程度（fail が増えない） |
| 行数 | `run_log.detail` の `snapshots +N` | 同時刻の VM 実行と同程度 |
| 所要 | run の duration | 15分前後、50分以内 |
| アップロード | run 末尾の `local raw=... gz≈...` | VM 実行と同じ桁（定常 60MB 前後） |
| 冪等 | 2回連続実行 | 2回目も `snapshots +N` が正、DB が壊れない |

GHA の egress は 2026-11-05 に実測済み（`3.42.157.194` / us-west-2）:
`https://estama.jp/` 200 / 2.0s、`/shop/34532/schedule/` 200 / 1.0s、
スクレイパ1店で cast 6行・fail 0、5秒間隔の連続アクセスでもブロックなし。
410 は存在しない shop id への正常応答（自宅 IP でも同じ）。403 / 0行が続く場合のみ
UA か IP ブロックを疑う。

### 代走（保険）として使う

```bash
gh workflow run collect.yml -f job=checkpoint -f mode=backup
```

`runner.py guard --max-age 90` が `status/last_ok.json` を読み、VM が90分以内に
成功していれば何もせず終了する。VM が落ちている時だけ動く。

VM が復帰した場合、checkpoint/ranking の冒頭で `_pull_if_remote_newer()` が
heartbeat と自分の最終成功を比較し、remote が新しければ先に pull する。
代走分の行が上書きで消えることはない。

### 主系を GHA に移す場合

1. `collect.yml` に schedule を追加（`mode: backup` ではなく `primary`、または guard 付き）
2. VM の cron を停止（`crontab -e` で checkpoint/ranking の行をコメントアウト）
3. 24h は `run_log` の欠損と `status/last_ok.json` の更新間隔を監視する

schedule は GitHub 側の遅延・欠落がある（実測: 連続未発火・36分遅延）。
毎時の穴を作らないため、主系を GHA にするのは schedule の挙動を測ってから。
