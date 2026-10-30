# estama-tracker

大阪のメンズエステ（estama.jp）で「普段は完売しているのに、今日は空いているキャスト」を拾うための完売追跡システム。出勤表を1時間おきに巡回して SQLite に蓄積し、完売の履歴と速度を分析する。

VM での運用手順は [RUNBOOK.md](RUNBOOK.md) にまとめてある。

## 構成

| ファイル | 役割 |
|---|---|
| `app/estama_scraper.py` | 出勤表の巡回。74店の当日分を取得し DB へ取り込む。完売スピード分析（`--speed-rank`）もここ |
| `app/ranking_scraper.py` | 週間人気セラピストランキング（14エリア×30人）の取得、`cast_rankings` 保存、ウォッチリスト同期（各エリア上位10位の店を `shops.json` へ追加） |
| `app/estama_tracker.py` | 分析 CLI（report / alert / sellout-forecast / monitor / notify / dashboard / cast）。引数解析とディスパッチ、取込・検証のみ。計算と整形は `queries.py` / `analysis.py` / `report_sections.py` に分離 |
| `app/queries.py` | R3 最終状態の SQL・日付/キャストキー解決・店名/エリア参照（分析側の共通クエリ） |
| `app/analysis.py` | アラート判定（普段完売なのに空き）・根拠行の組み立て・`daily_cast_status` ロールアップ |
| `app/report_sections.py` | レポート各節・完売予測（hazard）・監視・notify・HTML ダッシュボード |
| `app/common.py` | 共通部品（パス、UA、セッション、DB接続、JSON入出力、ステータス文言の正規化）。ここに一元化されている |
| `app/schema.sql` | DBスキーマ（shops / snapshots / cast_rankings / daily_cast_status / run_log / scrape_log / forecast_eval / slot_events / cast_watch） |
| `app/shops.json` | ウォッチリスト（巡回対象の店） |
| `app/banned_shops.json` | 出禁リスト（実行時に生成。git対象外） |
| `app/bakusai.py` | 爆サイ.com のスレッド発見・本文取得。専用 DB `bakusai.db`（git対象外、VMローカル） |
| `app/deep_shops.json` | deep tier（爆サイでスレッドが確認できた店だけの固定リスト。口コミ順位で埋めない） |
| `app/evidence.py` | alert の根拠表示（口コミ・爆サイ・枠消失・出典URL。表示専用で判定には使わない） |
| `app/RUNBOOK.md` | VM 運用手順書（1コマンド運用・デプロイ後の確認手順） |
| `runner.py` | 運用ランナー。下記の安全装置・DB検証・アーカイブ・publish を担当 |
| `storage.py` | 保存バックエンドの切替と検証（GitHub data ブランチ／R2／ローカルミラー） |

## 運用フロー

実行は Muse の VM で行い、成果物（DB）は現在は GitHub の `data` ブランチに置く（次ゴールで R2 へ移行）。Mac は閲覧・分析専用。

- **チェックポイント（1時間おき、毎時30分）**: `runner.py checkpoint`
  74店スクレイプ → DB取り込み → 検証 → `data` ブランチの `estama.db` を1コミットで上書き
- **ランキング（毎日 06:15）**: `runner.py ranking`
  ランキング取得＋ウォッチリスト同期 → 出禁判定 → DB・shops.json を `data` ブランチへ
- **日次アーカイブ**: 営業日（朝6時切替）が終わった時点で、**閉じた営業日**（期待営業日の前日）のDBを `archive/estama-<営業日>.db` に1回だけ保存。サイトが最終チェックポイントより先に日付を切り替えても取り逃さない。アーカイブ失敗時は検証不成立のキーを消して checkpoint/ranking は続行し、次回に再試行する

### runner の安全装置

- **同時実行ロック**: 全ジョブが単一ロックを共有。長引いた巡回と次回が重なると、後発は SKIP して正常終了する
- **アップロード前検証**: `integrity_check` と行数チェックを通らない DB は上げない。DB をスナップショットしてから本番キーへ1回書き込み、直後に blob SHA で照合する。不一致なら1回再送し、それでも不一致なら GitHub では前版 blob へ復元（前版が無ければキー削除）。壊れたファイルが正常な最新版を潰すことはない
- **publish 失敗の可視化**: 取込が成功していても publish に失敗した実行は `run_log` を `fail` にし、runner が exit 1 する（cron / 監視で気づける）
- **出禁ルール**: 観測開始から20日以上・観測できた日15日以上で、その間キャストの完売が一度も観測されなかった店は巡回対象から外す。`banned_shops.json` に理由付きで記録され、ランキング同期が再追加しても自動で外し直す。過去データは DB に残る

## セットアップ

```bash
python3 -m venv venv
./venv/bin/pip install requests beautifulsoup4   # boto3 は R2 バックエンドに切り替える場合のみ
```

保存先は `storage.json`（VM 側、リポジトリには含めない）で切り替える:

- `{"backend": "github", "repo": "yamahei21python/estama-tracker", "branch": "data"}` — GitHub の `data` ブランチにファイルとして保存（DB は gzip 圧縮、検証は blob SHA 照合）。**現在の暫定運用**（2026-10-26〜、R2 移行までの間）。認証は GitHub コネクタ経由で、VM にトークンは置かない
- R2 を使う場合は代わりに環境変数を設定する: `ESTAMA_R2_ENDPOINT` / `ESTAMA_R2_BUCKET` / `ESTAMA_R2_ACCESS_KEY_ID` / `ESTAMA_R2_SECRET_ACCESS_KEY`
- どちらも無ければ `r2_local/` へのローカルミラーで全フローが動く

## 手動実行

```bash
# 1店だけ取得（既定: 40900 SUPER AKUMA GIRLS）
./venv/bin/python app/estama_scraper.py --shop-id 40900 --import-db

# 全店巡回（当日分のみ）
./venv/bin/python app/estama_scraper.py --all-shops --import-db

# ランキング全エリア＋同期
./venv/bin/python app/ranking_scraper.py --area all --import-db --save-json --sync-watchlist

# 分析
./venv/bin/python app/estama_tracker.py counts --date 2026-10-27
./venv/bin/python app/estama_tracker.py sold-out --date 2026-10-27
./venv/bin/python app/estama_tracker.py alert --date 2026-10-27 --min-days 3 --threshold 0.7

# 日次ロールアップ再計算（runner が取り込み後に自動実行）
./venv/bin/python app/estama_tracker.py rollup

# 全セクション分析レポート（日次サマリー/ストリーク/アラート/完売予兆/
# タイミング/遷移/エリア/店舗/ランキング照合/ランキング変動/品質/
# カバレッジ監査/ウォッチ中cast/runner履歴）
./venv/bin/python app/estama_tracker.py report --date 2026-10-28

# お気に入りキャストのウォッチ登録（DB内テーブルなのでデータと一緒に残る）
./venv/bin/python app/estama_tracker.py watch add --shop-id 25081 --cast-id 362395
./venv/bin/python app/estama_tracker.py watch list
./venv/bin/python app/estama_tracker.py watch remove --shop-id 25081 --cast-id 362395

# push通知（ESTAMA_NOTIFY_WEBHOOK があれば POST、なくても本文を表示）
export ESTAMA_NOTIFY_WEBHOOK=https://discord.com/api/webhooks/...   # Slack/Discord両対応
./venv/bin/python app/estama_tracker.py notify

# HTMLダッシュボード生成（runner が毎job後に生成して data ブランチへ dashboard.html としてpush）
./venv/bin/python app/estama_tracker.py dashboard --out dashboard.html

# キャスト個別ビュー（出勤曜日・完売時刻・日別履歴・完売率）
./venv/bin/python app/estama_tracker.py cast --shop-id 25081 --cast-id 362395
```

## 予約判断の改善（slot_events / 予測 / 根拠 / 爆サイ / 監視）

```bash
# 枠イベントは取り込み時に自動記録（snapshots は不変）。再計算は rollup のまま
./venv/bin/python app/estama_tracker.py rollup

# 4時間以内に完売する確率（watch/deep 対象・n>=3・p>=0.5・上位5件）
# 履歴不足なら確率を出さずベースライン順位のみ
./venv/bin/python app/estama_tracker.py sellout-forecast --deep-shops app/deep_shops.json

# 監視レポート（店別fail率・0行店・checkpoint欠損・枠イベント取得率を冒頭に警告、exit 1）
# 既定日は現在の営業日（06:00切替）。--date は YYYY-MM-DD 厳格
./venv/bin/python app/estama_tracker.py monitor --db app/estama.db

# 爆サイ（別DB bakusai.db）。deep tier はスレッドが確認できた店だけの固定JSON
./venv/bin/python app/bakusai.py init
./venv/bin/python app/bakusai.py discover --deep-out app/deep_shops.json
./venv/bin/python app/bakusai.py crawl --deep app/deep_shops.json --limit 2 --max-pages 1
./venv/bin/python app/bakusai.py status
```

- 枠イベント: 30分枠の状態変化（○→×/─/TEL、戻り含む）だけを `slot_events` に1行。`slot_time` は表記と分（`27:00`=1620）を併記
- 爆サイ: 本文は先頭500字・投稿番号/時刻/アンカーID保存・アンカー連鎖は最大10ホップ・伏せ字は候補保存のみ。スレッドと店は多対多、alert帰属は「タイトル主店舗」＋「本文の完全名一致」だけ
- 根拠表示は表示専用。完売判定・予測の計算には一切使わない

## テスト

```bash
# 全テスト（外部通信なし。requests / beautifulsoup4 が必要）
for t in tests/test_*.py; do python3 "$t" || break; done

# 個別に回す場合
python3 tests/test_tracker_alert.py   # アラートの日次集計
python3 tests/test_final_states.py    # R3 最終状態（分析時のみ反転、DB非改変）
python3 tests/test_speed_rank.py      # 完売スピードの日跨ぎ順序（fetched_at 基準）
python3 tests/test_timestamps.py      # JST タイムスタンプ（UTC VM でのズレ回帰）
python3 tests/test_runner.py          # ロック・検証・アーカイブ・publish 失敗の exit 1
python3 tests/test_rollup.py          # daily_cast_status ロールアップとレポート節
python3 tests/test_prune.py           # 出禁ルール
python3 tests/test_slot_events.py     # 枠パース・イベント重複排除・遅延リプレイ拒否
python3 tests/test_hazard.py          # 時間帯別 hazard・ベースライン
python3 tests/test_evidence.py        # 根拠表示（口コミ/爆サイ/枠消失/出典）
python3 tests/test_bakusai.py         # 爆サイ（発見・紐付け・crawl・予算/リダイレクト）
python3 tests/test_monitor.py         # 監視の警告条件
python3 tests/test_schema_migration.py # user_version 移行・索引・クエリ計画
python3 tests/test_chrono.py          # 営業日06:00境界・chrono_keyとSQLの一致
python3 tests/test_gh_storage.py      # data ブランチ保存の blob 検証・復元
python3 tests/test_archive_retention.py  # アーカイブ保持（30日＋週次）
python3 tests/test_normalize.py       # ステータス正規化の真理表
python3 tests/test_report_sections.py # 未実行だった6節（timing/area/shops/ranking/forecast/print_report）
python3 tests/test_cli_wiring.py      # CLI 配線（rollup/report/dashboard/notify/counts/alert）
python3 tests/test_report_golden.py   # report/dashboard/notify のゴールデン（時計固定）
python3 tests/test_parse_golden.py    # 出勤表パースのゴールデン（今日/未来/境界HTML）
python3 tests/test_ranking_parse.py   # ランキングHTML fixture（PR/画像順位/リンク欠落の除外・DB取込）

# ゴールデンの意図的な更新（差分を確認してから）
# ESTAMA_UPDATE_GOLDEN=1 python3 tests/test_report_golden.py
# ESTAMA_UPDATE_GOLDEN=1 python3 tests/test_parse_golden.py

# デモ
./venv/bin/python app/estama_tracker.py demo
```

## データの注意（重要）

- **意味があるのは当日分だけ**。未来日の行はサイト側のテンプレート表示で、ステータス（特に完売）は実態を表さない。そのため取得は `--days 1`（当日分）が既定
- **完売の判定はステータス文言からのみ**行う。枠の ○/×/─ は使わない（未来日のダミー対策）
- **シフト表示は完売・受付終了になるとサイト側で消える**。その場合の `work_hours` は枠からの推定値で、実シフトより短く潰れる（完売組の平均約2.5h）。勤務時間の分析では推定値を実シフトと混同しないこと
- **アラートの履歴は「日数」で数える**。1日の状態はその日の最終観測で代表させる。チェックポイントの回数で水増しはしない
- **最終状態は分析時にのみ R3 反転する**（`final_rows` / `counts` / `sold-out` / `alert` 経由）。サイトは終業時に全キャストを `closed` へリセットする（店全体フリップは退勤後30分前後、個人終業は shift_end+30分前後）ため、最終観測が `closed` の場合は**その日の最後の non-closed 観測を最終状態とする**（終日 closed なら closed のまま）。DB 行は生のまま。終業時刻の分析は `status_raw` / `shift_end` の生データを使う。日跨ぎの時系列は `fetched_at`（`YYYY-MM-DD HH:MM:SS JST`）順で判定する（`fetched_time` 単独は 00-05 時台が誤順になる）
- **完売スピード分析（`--speed-rank`）も `fetched_at` 基準**。`target_date` は営業日なので 00-05 時台の checkpoint は翌暦日だが同一 target に入る。旧実装は `MIN(fetched_time)` の文字列比較で 13:33 完売を「00:33」と誤報告していた（実データ21件該当）。06:00未満の完売は「深夜完売」バッジ（超速判定に入れない）
- **集計キーは `cast_id`**（`COALESCE(cast_id, cast_name)`）。R3 partition・alert 履歴/対象・speed-rank の group by すべて。同一 cast_id の改名（実績: 953092 が1日内で2名前）を2人に数えないため。counts の分母は `counts --date 2026-10-26` = 920（旧 921 は改名分裂の誤り）
- **`全コマ×(事前予約不可/おとり候補)` = `dummy`**。当日で文言なし・全枠×のキャストは予約不可かつおとり候補。旧 `unknown` の14行は初回接続時の移行（`_migrate_reclassify_decoy`）で `dummy` に再分類（`status_raw` は不変）。未来日の全全枠×は従来通り `unknown`（実状態が不明なため）
- **シフト時刻の文字列比較は禁止**。`shift_start`/`shift_end` は非ゼロ埋め（`9:00`）＋24時超表記（`29:00` = 翌5時）のため、`'9:00' > '23:00'` が文字列比較で真になる。`common.parse_clock()`（分単位 int）で比較すること。`work_hours` は既に正規計算済み
- `shops.json` の `area` は情報項目で、取り込みロジックは参照しない（初期店は店舗の地域、ランキング同期で追加された店はランキングのエリア名が入る）。`report` のエリア別完売率集計で使用
- **`daily_cast_status`** … R3 で確定した「キャスト×日×最終状態」を1行に集約した日次ロールアップ（アラート履歴・トレンド・遷移分析の基盤）。`first_sold_out_at` に当日最初の sold_out 観測時刻を保持し、`report`【5】で常連の完売時刻を集計。`estama_tracker.py rollup` が `DELETE+INSERT` で再計算するため部分日も何度でも再取得可。runner は checkpoint/ranking 取り込み後に自動実行
- **`run_log`** … runner ジョブの実行履歴（`ok|fail|skip`＋detail）。巡回失敗・ロック競合の監査用。`report`【12】で閲覧。2時間超 `running` のまま残る行は runner 死亡の可能性として強調表示される
- **`scrape_log`** … 店舗ごとの巡回結果（`ok|empty|fail`＋行数＋エラー内容）。「取得失敗」と「出勤0人」を区別するための監査テーブル。runner 経由なら `run_id` で `run_log` と結合できる（runner が `ESTAMA_RUN_ID` を環境変数で渡す）。`report`【12】で時間帯別 ok/empty/fail と直近の異常を閲覧
- **巡回欠損とアラート**: `report`【11】が店ごとの巡回カバー時間数の中央値から異常にカバーが低い店を検出し、【3】アラートに `<<巡回欠損>>` を併記する（取得不足による誤報防止）
- **ランキング同期の watchlist 自動追加**: `ranking_scraper.py --sync-max-rank N` で上位 N 位までの未登録店を `shops.json` に追加（`source: ranking`）。runner は 30 で呼ぶ
- **`was_low` / `forecast_eval`** … `daily_cast_status.was_low` は「初完売観測前（または完売なし日の全体）で low を観測」した cast-day に1。`forecast_eval` は rollup が日ごとに `was_low → sold_out` の的中数を書く自己採点テーブルで、【4】に推移を表示
- **`cast_watch`** … お気に入りキャストのDB内watchlist。`watch add/remove/list` で管理（cast_id優先のキー解決）。report【12】が今日の状態＋曜日別出勤傾向を表示し、`notify` の対象にもなる
- **ランキング変動** … report【9b】がエリア2700（大阪全体）の直近2スナップで ±5位以上の変動・新規IN・圏外OUTを検出。`notify` の通知対象
- **push通知** … `notify` コマンドがアラート+ウォッチhit/完売+予兆採点+ランキング変動+枠消化の勢い+新規cast+runner失敗を1メッセージにして `ESTAMA_NOTIFY_WEBHOOK` へPOST（Slack/Discord両方で使える `text`/`content` 併記payload）。未設定でも本文を表示。runnerは各job後にbest-effortで呼ぶ（失敗してもjobは落とさない）
- **HTMLダッシュボード** … `dashboard` コマンドが外部依存ゼロの静的HTMLを生成。runnerは publish() でDBアップロード後に `dashboard.html` として data ブランチへpushするので、htmlpreview 等でブラウザから見られる。曜日×時刻の完売ヒートマップ（first_sold_out_at 全集計）つき
- **予約の勢い（枠消化）** … `slot_pattern` の `×Nコマ`（予約済み枠）の時系列増分で「予約が入っている」勢いを検出。low より早く燃える兆候を report【4b】/dashboard/notify で表示
- **新規cast・移籍検出** … report【9c】が当日初登場のcast_idと、直近14日で複数店舗に出現するcast_id（移籍候補）を一覧化
- **cast コマンド** … `cast --shop-id X --cast-id Y` が個別キャストの出勤曜日傾向・初完売時刻の分布・直近日別・累計完売率を表示。「何時までに予約すべきか」の判断材料
- **「時間のみ」ステータス**: サイト側がステータス文言を設定していないキャスト（予約枠0・空き枠0）にスクレイパーが付けるラベル。ダミー/架空人物の可能性が高く、`closed`（受付終了相当）に分類される。ステータス文言がない限り `sold_out` にはならない（ダミー対策）
- **「TELのみ」= `dummy`**: タイムテーブルのセルが全て `TEL`（電話のみ＝オンライン予約枠なし）のキャスト。`status_norm` は **`dummy`**（`available`/`closed` にはしない）。`slot_pattern` は `全Nコマ TEL (電話のみ)`。サイト文言がある場合も `文言 / TELのみ` にして `dummy`（完売文言があれば `sold_out` 優先）。列は `td[data-idx]=cast_id` で縦に紐付ける
