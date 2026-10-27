# estama-tracker

大阪のメンズエステ（estama.jp）で「普段は完売しているのに、今日は空いているキャスト」を拾うための完売追跡システム。出勤表を1時間おきに巡回して SQLite に蓄積し、完売の履歴と速度を分析する。

## 構成

| ファイル | 役割 |
|---|---|
| `app/estama_scraper.py` | 出勤表の巡回。74店の当日分を取得し DB へ取り込む。完売スピード分析（`--speed-rank`）もここ |
| `app/ranking_scraper.py` | 週間人気セラピストランキング（14エリア×30人）の取得、`cast_rankings` 保存、ウォッチリスト同期（各エリア上位10位の店を `shops.json` へ追加） |
| `app/estama_tracker.py` | 分析側。完売一覧・ステータス件数・「普段完売なのに今日空いている」アラート |
| `app/common.py` | 共通部品（パス、UA、セッション、DB接続、JSON入出力、ステータス文言の正規化）。ここに一元化されている |
| `app/schema.sql` | DBスキーマ（shops / snapshots / cast_rankings） |
| `app/shops.json` | ウォッチリスト（巡回対象の店） |
| `app/banned_shops.json` | 出禁リスト（実行時に生成。git対象外） |
| `runner.py` | 運用ランナー。下記の安全装置と R2 アップロードを担当 |

## 運用フロー

実行は Muse の VM で行い、成果物（DB）は Cloudflare R2 に置く。Mac は閲覧・分析専用。

- **チェックポイント（1時間おき、毎時30分）**: `runner.py checkpoint`
  74店スクレイプ → DB取り込み → 検証 → R2 の `estama.db` を上書き
- **ランキング（毎日 06:15）**: `runner.py ranking`
  ランキング取得＋ウォッチリスト同期 → 出禁判定 → DB・shops.json を R2 へ
- **日次アーカイブ**: 営業日（朝6時切替）が終わった時点で、前営業日のDBを R2 の `archive/estama-<営業日>.db` に1回だけ保存

### runner の安全装置

- **同時実行ロック**: 全ジョブが単一ロックを共有。長引いた巡回と次回が重なると、後発は SKIP して正常終了する
- **アップロード前検証**: `integrity_check` と行数チェックを通らない DB は上げない。アップロードは「ステージング → サイズ検証 → 本番キーへ昇格」の順で、破損ファイルが正常な最新版を潰すことはない
- **出禁ルール**: 観測開始から20日以上・観測できた日15日以上で、その間キャストの完売が一度も観測されなかった店は巡回対象から外す。`banned_shops.json` に理由付きで記録され、ランキング同期が再追加しても自動で外し直す。過去データは DB に残る

## セットアップ

```bash
python3 -m venv venv
./venv/bin/pip install requests beautifulsoup4 boto3
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
```

## テスト

```bash
python3 tests/test_tracker_alert.py   # アラートの日次集計（標準ライブラリのみ）
python3 tests/test_final_states.py    # R3 最終状態（分析時のみ反転、DB非改変）
python3 tests/test_speed_rank.py      # 完売スピードの日跨ぎ順序（fetched_at 基準）
python3 tests/test_runner.py          # ロック・検証・アーカイブ・アップロード
python3 tests/test_prune.py           # 出禁ルール
./venv/bin/python tests/test_normalize.py  # ステータス正規化の真理表
./venv/bin/python app/estama_tracker.py demo
```

## データの注意（重要）

- **意味があるのは当日分だけ**。未来日の行はサイト側のテンプレート表示で、ステータス（特に完売）は実態を表さない。そのため取得は `--days 1`（当日分）が既定
- **完売の判定はステータス文言からのみ**行う。枠の ○/×/─ は使わない（未来日のダミー対策）
- **シフト表示は完売・受付終了になるとサイト側で消える**。その場合の `work_hours` は枠からの推定値で、実シフトより短く潰れる（完売組の平均約2.5h）。勤務時間の分析では推定値を実シフトと混同しないこと
- **アラートの履歴は「日数」で数える**。1日の状態はその日の最終観測で代表させる。チェックポイントの回数で水増しはしない
- **最終状態は分析時にのみ R3 反転する**（`final_rows` / `counts` / `sold-out` / `alert` 経由）。サイトは終業時に全キャストを `closed` へリセットする（店全体フリップは退勤後30分前後、個人終業は shift_end+30分前後）ため、最終観測が `closed` の場合は**その日の最後の non-closed 観測を最終状態とする**（終日 closed なら closed のまま）。DB 行は生のまま。終業時刻の分析は `status_raw` / `shift_end` の生データを使う。日跨ぎの時系列は `fetched_at`（`YYYY-MM-DD HH:MM:SS JST`）順で判定する（`fetched_time` 単独は 00-05 時台が誤順になる）
- **完売スピード分析（`--speed-rank`）も `fetched_at` 基準**。`target_date` は営業日なので 00-05 時台の checkpoint は翌暦日だが同一 target に入る。旧実装は `MIN(fetched_time)` の文字列比較で 13:33 完売を「00:33」と誤報告していた（実データ21件該当）。06:00未満の完売は「深夜完売」バッジ（超速判定に入れない）
- `shops.json` の `area` は情報項目で、ロジックは参照しない（初期店は店舗の地域、ランキング同期で追加された店はランキングのエリア名が入る）
- **「時間のみ」ステータス**: サイト側がステータス文言を設定していないキャスト（予約枠0・空き枠0）にスクレイパーが付けるラベル。ダミー/架空人物の可能性が高く、`closed`（受付終了相当）に分類される。ステータス文言がない限り `sold_out` にはならない（ダミー対策）
- **「TELのみ」= `dummy`**: タイムテーブルのセルが全て `TEL`（電話のみ＝オンライン予約枠なし）のキャスト。`status_norm` は **`dummy`**（`available`/`closed` にはしない）。`slot_pattern` は `全Nコマ TEL (電話のみ)`。サイト文言がある場合も `文言 / TELのみ` にして `dummy`（完売文言があれば `sold_out` 優先）。列は `td[data-idx]=cast_id` で縦に紐付ける
