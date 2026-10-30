# リファクタリング計画（2026-10-30 計測版）

計測日: 2026-10-30 JST。数値はその時点のスナップショット。
前提: 出力（report / dashboard / notify / alert）は**不変**とする。振る舞いを変える修正は別タスク扱い。

## 0. 結論

最大の負債はコードの複雑さではなく、**「GitHub data ブランチをDB置き場にしていること」**と**「スキーマだけ移行が止まっていること」**。
この2つは tracker 分割より先に、小さな差分で直せる。R2 移行は「次のゴール」ではなく数週間以内の必須項目。

## 1. 計測: 規模とホットスポット

| ファイル | 行数 | 関数 | print箇所 | 最大関数（行/分岐） |
|---|---:|---:|---:|---|
| `app/estama_tracker.py` | 2,228 | 60 | 58 | `main` 240L/50br |
| `app/bakusai.py` | 1,126 | 49 | — | `discover` 109L/31br（7引数） |
| `app/estama_scraper.py` | 1,066 | 19 | 60 | `_parse_schedule_html` 154L/51br |
| `runner.py` | 705 | 48 | — | 保存系で219L（全体の31%） |
| `app/ranking_scraper.py` | 326 | 6 | — | `scrape_area_ranking` 91L/15br |
| `app/evidence.py` | 313 | — | — | — |
| `app/common.py` | 247 | 11 | — | — |

tracker の続き: `build_dashboard` 131L/33br、`monitor_report` 123L/38br、`sellout_forecast` 97L/30br/**8引数**、`build_notify_text` 95L/45br、`rollup_daily` 86L、`print_report` 83L/28br。
scraper の続き: `show_speed_ranking` 124L/20br、`main` 123L/27br、`init_session` 88L/24br。

実行コスト実測（2日分DB）: `report` 0.34s / 204行、`dashboard` 0.19s / 24KB。行数に対しほぼ線形なので、30日蓄積で数秒台が見込まれる。

## 2. 計測: テストの実効カバレッジ

- スイート全体: 15ファイル・267アサーション・**約2.3秒**（うち `test_runner.py` 1.16s）。
- tracker: 名前参照で未テスト **24/55関数**。
- tracker のうちテストから**一度も実行されない**もの:
  `section_timing` / `section_area` / `section_shops` / `section_ranking` / `section_sellout_forecast` / `print_report` / `run_demo` / `print_verify` / `load_snapshot` / `_date_arg` / `_resolve_cast` / `_badge`。
- テストが `t.` 経由で触る tracker シンボルは **6個**（`monitor_report`, `alert_evidence_lines`, `_checkpoints_expected`, `section_monitor`, `main`, `load_deep_shop_ids`）。
  → 分割時の互換面は小さい。
- `ranking_scraper.py` は**テスト参照ゼロ**。壊れても静かに巡回対象（shops.json）が痩せる。
- bakusai は名前参照で21/39未テストだが、`crawl`/`discover` 経由で間接的に多くが走る。
- CLI: tracker のサブコマンドは15個。`main()` をテストが呼ぶのは `monitor` のみ。`status/load/sold-out/counts/alert/rollup/watch/notify/dashboard/cast` は「中身は部分的にテスト済み、argparse配線は未検証」。
- `final_rows`（R3の中核）は本番コードから呼ばれず**テスト専用**。本番は `_final_sql` を直接埋め込む。
- テスト衛生: `/tmp/estama-*` のスクラッチが7ディレクトリ残留。`tests/test_monitor.py:15` と `tests/test_prune.py:17` は固定パス（並列実行・失敗時に汚染しうる）。267アサーション中16個（6%）が死んだ `gemini_retry_guard` のテスト。

## 3. 計測: 問い合わせ層の重複

- `SELECT shop_id, name FROM shops` 相当: **10箇所**。
- `target_date = ?`: **17箇所**。
- cast_key `CAST(COALESCE(cast_id, cast_name) AS TEXT)`: **12関数**。
- 完全一致の重複SQL断片: **11組**。tracker の SELECT は計83本。
- `daily_cast_status`（R3確定の日次集約）を読むのは **4関数**、`snapshots` を直接読むのは **24関数**。
  → 集約テーブルの意味が半分しか活きておらず、各セクションが生スナップショットから再実装している。

## 4. 計測: 意味論の分裂

| 概念 | 実装数 | 場所 |
|---|---:|---|
| 時系列キー | 3 | tracker は素の `fetched_at` / scraper は `COALESCE(fetched_at,'9999-12-31 '‖fetched_time)`+snapshot_id / evidence は独自 ORDER BY |
| 営業日（6時切替） | 4 | runner 定数 `BUSINESS_DAY_FLIP_HOUR` / tracker `timedelta(hours=6)` / tracker SQL内 `'T06:00:00'` / scraper `now_jst()-6h` |
| `now_jst` 定義 | 3 | `common.py` / `runner.py` / `bakusai.py` |
| JST リテラル | 3 | `ZoneInfo("Asia/Tokyo")` × 3 |

import時にパスを確定している箇所: `bakusai.py:48`、`evidence.py:25-28`、`runner.py:50`、`common.py` の `DB_PATH`。
テストは env を import 前に差す方式（`test_gh_storage.py:12`）で回避しており、`evidence._conns`（唯一のモジュール可変状態）もこの制約の産物。パスは遅延関数にすべき。

枠単位スナップショットを追加した時点で、時系列キーの4実装目が生まれる構造になっている。

## 5. 計測: データプレーン（最重要）

| 実測 | 値 |
|---|---:|
| ローカルDB | 17,401行 / 8.9MB（535 B/行） |
| 本番 `estama.db.gz`（dataブランチ先端） | **5.26MB**（ローカルの約3倍のデータ量） |
| 1時間あたりの増分 | 5.19 → 5.26MB（**約+70KB/h ≒ +1.7MB/日**） |
| 日次アーカイブ | 10-26: 0.74MB → 10-27: 2.16MB → **10-28: 3.82MB** |
| dataブランチ | 毎時1コミット（:38〜:40）で DB全体の新blob、さらに1日1本アーカイブblob |
| 履歴の残骸 | `estama.db.staging.gz` の add/delete が履歴に堆積 |
| .git サイズ | 89MB（大半が data ブランチ blob） |

gzip済みblobはgitのdelta圧縮がほぼ効かないため、履歴は**約120MB/日**（DB 5.2MB×24h＋アーカイブ）で単調増加する。
月3GB超、GitHubの推奨上限5GBに数ヶ月で到達。R2 移行時に保持ポリシー（例: 直近30日＋週次）を決めること。

## 6. 計測: スキーマのドリフト

コードは `fetched_at` / `cast_id` へ移行済みだが、**インデックスとUNIQUE制約は移行前のキー列を指したまま**。

| インデックス/制約 | 定義 | 実際のコード | 結果 |
|---|---|---|---|
| `idx_snapshots_time` | `(target_date, fetched_time)` | 時系列はすべて `fetched_at` | **全 ORDER BY が temp b-tree**（EXPLAIN で確認） |
| `idx_snapshots_cast_date` | `(shop_id, cast_name, target_date)` | 集計キーは `cast_id` / cast_key | cast_id 検索に使えない |
| `UNIQUE(shop_id, cast_name, target_date, fetched_time)` | cast_name + fetched_time | 同上 | 制約も旧キー |
| `idx_snapshots_work_hours` | `(work_hours)` 単独 | これ単独で絞るクエリなし | 書き込みコストのみ |

`PRAGMA user_version` 不在の直接的帰結。`(target_date, fetched_at)` インデックス追加だけで報告系のソートが消える（投資効率が最も高い修正）。

## 7. 診断

1. **データプレーンの寿命**が最短の制約。GitHub data ブランチは約120MB/日の履歴増加で数ヶ月で運用不能。設計都合ではなく事業継続の問題。
2. **スキーマ移行の負債**。旧キーのインデックス＋版管理なしは「旧DB×新コード」で静かに遅くなる。R2 と `user_version` は同時が最短。
3. **tracker の「取得＋計算＋整形」同居**が未テスト24関数の直接原因（print 58箇所）。ただしテスト2.3秒・report 0.34秒である限り緊急性は低い。
4. **問い合わせ層の不在**が重複と意味論分裂の親。各セクションが時系列キー・cast_key・営業日・整形をその場で再発明している。
5. **保存層が runner に埋まっている**（705行中219行）。R2 移行＝runner 本体の手術になり、次のゴールの初手が最も危険な場所に集中する。

## 8. 計画 v3

| 段 | 内容 | 根拠 | 見積 |
|---|---|---|---|
| **0** | 死骸退避（`gemini_retry_guard.py`+test、`app/analyze_*.py` 5本＋`inv_rank.py` を `scripts/oneoff/` へ）、`/tmp` 固定パス2件を mkdtemp 化、CI 追加 | 誤った旧規約（fetched_time前提SQL）の拡散防止。即効 | 1h |
| **1** | スキーマ移行: `(target_date, fetched_at)` インデックス追加、`work_hours` インデックス削除、旧キーのインデックス/UNIQUE整理、`PRAGMA user_version` 導入 | 全 ORDER BY のソート除去。版管理の土台 | 2h |
| **2** | storage 抽出（`runner.py` → `storage.py`）＋**R2 移行**＋保持ポリシー（アーカイブは R2 のみ等） | data ブランチの寿命。次ゴールそのもの。runner は約480行へ | 半日〜1日 |
| **3** | 時間基礎の一本化（`common.business_date` / `common.chrono_key` / 遅延パス）＋契約テスト（真理表） | 枠スナップショット導入で4実装目が生まれる前に | 半日 |
| **4** | `queries.py`（重複SQL 11組・cast_key 12箇所・shops名引き10箇所の集約）→ tracker 分割（compute/format 分離＋facade） | 未テスト24関数の解消 | 1〜2日 |
| 任意 | ranking fixture（HTML 1枚で `scrape_area_ranking` を固定）、`_conns` 遅延化、`sellout_forecast`/`bakusai.discover` のパラメータオブジェクト化、`daily_cast_status` への読み替え拡大 | 静かな劣化防止 | 随時 |

順序の根拠: 段2が「次のゴール（本番DB・デプロイ同期）」そのもの。段4は最後でよい。

## 9. 前提条件・リスク

- **移動前キャラクタリゼーション（段4の条件）**: 未実行の6セクション（timing/area/shops/ranking/sellout_forecast/print_report）について、fixture DB で全セクションを実行しキー行と exit code を assert するスモークを先に追加する。これなしの分割は「動いているか不明なコードの引っ越し」。
- **ゴールデン出力（段4の合格条件）**: 分割前に report / dashboard / notify の出力を固定し、分割前後で一致すること。既存15テストは無変更で緑。
- **`PRAGMA user_version`（段1）**: `common.connect_db` に順序付き migration 表。bakusai のインライン SCHEMA も同方式に揃える。
- **R2 保持ポリシー（段2の意思決定）**: 日次アーカイブを何日残すか。R2 は容量課金が軽いが、無期限は避ける（例: 直近30日＋週次）。
- **VMデプロイ**: 実施はユーザー。段1のスキーマ移行は本番DB（data ブランチ）に対しても一度だけ走るため、移行は冪等に書く。
- **`daily_cast_status` への寄せ**: 段4で集約テーブルの読み手を増やすかは別判断。寄せると R3 反転の呼び出し点と時系列分裂が減る（推奨、ただし出力差分の確認が前提）。

## 10. 受入基準（数値）

1. `report` が30日DBで **< 1.0s**。
2. data ブランチ（または R2）の増加が「DB上書き＋保持N日」で**有界**。
3. 主要関数 **< 80行 / 分岐 < 20**（`main`, `_parse_schedule_html`, `build_dashboard`, `monitor_report`, `sellout_forecast`, `discover` が対象）。
4. tracker の未テスト関数が **24 → 12以下**。CLI 配線（rollup/report/dashboard/notify）に最低1本のテスト。
5. CI（compileall＋全テスト）が push 条件で緑。スイートは5秒以内を維持。

## 11. 非目標

- LLM 利用、口コミの再取得、過去分バックフィル、外部への即時通知。
- 完売判定・予測アルゴリズムの変更（evidence は表示専用のまま）。
- 爆サイの取得ルール変更（1.5〜2.0秒間隔・429 Retry-After・500req/日・再開可能・伏せ字は候補保存のみ・alert帰属は confidence ≥ 0.8 のタイトル主店舗＋本文完全名一致）。
- deep tier を口コミ順位で埋めること（スレッド確認店のみ）。
- VM へのデプロイ実施（ユーザーが行う）。
