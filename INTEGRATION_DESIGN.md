# estama-tracker × 口コミ × 爆サイ 統合設計まとめ

## 0. 目的

estama-tracker の「普段完売なのに今日空いているキャスト」alert に、**口コミの特徴・サービスタグ・爆サイの話題**を付与し、狙うべき子の優先度と根拠を 1 画面で判断できるようにする。

## 1. データソース

| ソース | 状態 | 用途 |
|---|---|---|
| estama.jp | 稼働中 | 完売データ（本体） |
| men-esthe.jp | 403（取得不可） | — |
| menesreview.db（既存ローカル） | 取得済み | men-esthe.jp 口コミ（appearance/technique/service/cost_performance/hospitality/repeat_intention + 本文） |
| men-esthe-review.com（メンエスノート） | 無料分のみ取得可 | 口コミ本文・スコア・キャスト名（プラチナはゲート） |
| 爆サイ | 取得可 | 話題量・引用スニペット |
| kuchikomi（cityheaven.net） | JSON 2240件 | サービスタグ抽出の手法参考（fact-atom 7型）。メンエス店舗との直接マッチは薄い |

## 2. DB 一覧（フルパス）

### estama-tracker 側
- `/Users/kohei/Myproject/estama-tracker/app/estama.db`
- `/Users/kohei/Myproject/estama-tracker/app/schema.sql`

### 口コミ DB（menesureview）
- `/Users/kohei/Myproject/menesureview/menesreview.db`
- `/Users/kohei/Myproject/menesureview/kagawa_menes.db`
- `/Users/kohei/Myproject/menesureview/minesreview.db`（空）
- `/Users/kohei/Myproject/menesureview/mesreview.db`（空）
- `/Users/kohei/Myproject/menesureview/ヴィオテラス_口コミ.csv`

### その他関連
- `/Users/kohei/Myproject/kuchikomi/data/{prefecture}.json`
- `/Users/kohei/Myproject/estama-tracker/r2_local/`

## 3. DB 設計

### 3.1 既存 DB の再利用
- `menesreview.db` を **正** とする。estama-tracker 側は `ATTACH DATABASE ... AS review` で読み取り結合。
- `kagawa_menes.db` の `shop_aliases`（電話/URL/名前で 73.4% 紐付け済み）を店舗マッチに再利用。

### 3.2 新規テーブル（estama-tracker 側 DB）

CREATE TABLE bakusai_threads (
  thread_id    TEXT PRIMARY KEY,
  shop_id      INTEGER,
  title        TEXT,
  area_code    TEXT,
  view_count   INTEGER,
  post_count   INTEGER,
  last_post_no INTEGER,
  fetched_at   TEXT
);

CREATE TABLE bakusai_posts (
  thread_id   TEXT NOT NULL,
  post_no     INTEGER NOT NULL,
  posted_at   TEXT,
  body        TEXT,
  reply_to    INTEGER,
  fetched_at  TEXT,
  PRIMARY KEY (thread_id, post_no)
);

CREATE TABLE bakusai_mentions (
  thread_id  TEXT NOT NULL,
  post_no    INTEGER NOT NULL,
  cast_id    TEXT NOT NULL,
  match_type TEXT NOT NULL,
  PRIMARY KEY (thread_id, post_no, cast_id)
);

CREATE TABLE bakusai_shop_map (
  thread_id   TEXT PRIMARY KEY,
  shop_id     INTEGER,
  confidence  REAL,
  source      TEXT
);

CREATE TABLE cast_features (
  cast_id       TEXT PRIMARY KEY,
  features      TEXT,
  service_tags  TEXT,
  summary       TEXT,
  computed_at   TEXT
);

### 3.3 キャスト辞書の安定化
- `cast_id = (shop_id, normalized_name)` のハッシュ。
- 辞書ソース: `snapshots` / `cast_rankings` の distinct `(shop_id, cast_name)`。
- normalized_name: NFKC → 空白除去 → さん/ちゃん/くん 除去 → カタカナ↔ひらがな統一 → 長音符統一。

## 4. マッチング戦略

### 4.1 口コミ ↔ キャスト（menesreview）
- `reviews.staff_name` を normalized_name で突合。
- shop は `shop_aliases` 経由で estama `shop_id` に解決。

### 4.2 爆サイ ↔ キャスト（逆引き）
1. **exact**: normalized 完全一致
2. **normalized**: 上記 + カタカナ/ひらがな・長音符統一
3. **initial**: 伏せ字対応（先頭1文字 + 文字数が一致する辞書候補を candidate として保存、自動確定しない）

伏せ字マッチは alert に含めない（ノイズ回避）。

### 4.3 店舗スレ ↔ shop_id
- 爆サイ店舗ページの電話番号を最強キー → `shops` の電話と突合。
- フォールバック: 店名ファジー一致 + 手動マップ。

## 5. サービスタグ抽出

SERVICE_KEYWORDS = {
  "手コキ":      ["手コキ", "TK", "手コ"],
  "ディープリンパ": ["ディープ", "ディープリンパ", "深リンパ"],
  "SKR":        ["SKR", "エスカー"],
  "DK":         ["DK", "ディープキス"],
  "本番":        ["本番", "IN", "挿入", "中出し"],
  "キス":        ["キス"],
  "鼠径部":      ["鼠径", "キワ", "際"],
}

- 口コミ本文 + 爆サイ本文からキーワード出現をカウント。
- 集計は「そのキャストの口コミ N 件中 M 件で言及」の比率。
- 閾値以上で ○、一部で △、ゼロで ×。

## 6. alert 出力イメージ

[今日空いてる] 浅田もな @FROG SPA  (普段完売率 0.82)
  口コミ(n=12): 見た目4.6 / 技術4.2 / サービス4.5 / リピート4.7
  特徴: スタイル抜群 / 揉みが強い / 話が面白い
  サービス: SKR ○ / ディープリンパ ○ / DK △ / 本番 × / キス △
  爆サイ: 直近7日 言及3件 (最新 10/25)
    「吉川入ってみたけどありゃ売れるわ」
    「Hカップなんか、個人的にはデカすぎるからパスやな」

## 7. 実装フェーズ

### Phase 1（最小・lazy）: 口コミ結合
- `estama_tracker.py alert` に `--review-db` オプション追加。
- ATTACH DATABASE で menesreview.db を読み取り結合。
- 口コミスコア + 件数 + サービスタグ（辞書集計）を出力。

### Phase 2: 爆サイ差分取得
- `bakusai_scraper.py` 新設（日次バッチ）。
- スレッド一覧 → 店舗スレ特定 → レス差分取得 → mentions 生成。
- alert に言及回数・最新スニペットを追加。

### Phase 3: LLM 特徴要約
- キャストごとに口コミ本文を LLM に投げ、特徴3フレーズ + 1-2文要約を生成。
- `cast_features` にキャッシュ（再計算しない）。

### Phase 4: メンエスノート追加ソース
- `menesreview.db` の `reviews` に `source` カラム追加。
- メンエスノート スクレイパーで無料口コミを差分追加。
- サービスタグ辞書を拡充。

## 8. 地域データ確認結果

大阪データは十分に存在する。

### kagawa_menes.db
- 大阪エリアの店舗: **1,512店**
- 大阪エリアの口コミ: **48,198件**
- 全体の店舗 1,732 中、大阪が圧倒的多数

### menesreview.db
- 大阪府の店舗（stores）: **908店**

### 地域判定の注意
- `area` カラムは細かい粒度（「日本橋（大阪）」「堺筋本町・本町」など）。
- 大阪判定は `area LIKE '%大阪%'` だけだと漏れる。複数キーワード OR が必要。
- estama-tracker 側で地域別 alert を出すなら、`shops.area` を正規化した `region` 列を追加するか、キーワード辞書で判定する。

## 9. 実装上の注意点

- **men-esthe.jp は 403** → menesreview.db の過去分で代替。
- **爆サイは HTML が重い** → 日次1回、店舗スレのみ。
- **表記ゆれ** → menesreview の `normalize.py` を import して統一。
- **伏せ字** → 自動確定しない（候補保存のみ）。
- **LLM コスト** → キャストごとに1回、キャッシュ必須。
- **アクセス頻度** → 1-2秒ランダム待機、429 時 Retry-After 対応。

## 10. 未決事項

1. **DB の正**: menesreview.db（口コミ詳細） vs kagawa_menes.db（estama 統合済み）。後者なら店舗紐付けが不要。
2. **爆サイ取得範囲**: estama 登録店に絞るか全店舗スレを追うか。
3. **サービスタグ粒度**: 上記6カテゴリで十分か。
4. **メンエスノートのスクレイプ頻度**: 日次 or 週次。

## 11. 推奨着手順

**Phase 1 → Phase 2 → Phase 3 → Phase 4** の順。Phase 1 は ATTACH DATABASE 1行 + スコア表示で最小実装でき、すぐ alert の価値が上がる。各フェーズ完了後に次を判断。

## 12. アンカー遡り（Backtracking）による爆サイ深掘り

爆サイの価値は「単発のレス」ではなく「レス間の関係（アンカー）と文脈」にある。

### 12.1 アンカー解析

- `>>数字` パターンを regex で抽出 → `bakusai_posts.reply_to` に保存
- `backtrack(post)` でアンカーを遡り、会話チェーンを再構成
- 兄弟レス（同一 reply_to を持つレス群）を集約

### 12.2 実例: #212 → #205 → #201

```
#212  2026/10/25 11:50   >>205  行ったらわかる。じゅっぽじゅぽ。
#205  2026/10/24 19:49   >>201  具体的にプリーズ
#201  2026/10/24 14:42   >>183  色々と気持ち良すぎてとろけそう。
```

- #212 単体では「じゅっぽじゅぽ」しか分からず意味不明
- 遡ることで #201 のサービス話題が復元される
- 文脈付きでキャスト特徴を抽出可能

### 12.3 実例: #191 → #193 / #194（キャスト「姫宮」のスケジュール情報）

```
#191  2026/10/23 08:17   姫宮ちゃん次の出勤いつアップするんやー
#193  2026/10/23 08:48   >>191  アップしたらもう埋まってるからほんしが姫予約してるんやで
#194  2026/10/23 13:19   >>191  おまえには教えてあげない。みんな帰る時，次回予約して帰るからホームページには完売しか出ない
```

- キャスト名: 姫宮
- シグナル: スケジュール情報（出勤アップのタイミング、次回予約の取り方）
- 重要な発見: 「ホームページには完売しか出ない」→ estama の snapshots では「完売」しか見えないが、爆サイでは「いつアップされるか」「次回予約の取り方」が共有されている

### 12.4 実例: #195 → #197（新人報告）

```
#195  2026/10/23 13:47   新人と8585してきた！
#197  2026/10/23 09:20   >>195  ええて、
```

- キャスト名: 不明（「新人」）
- シグナル種別: 新人入店報告
- estama-tracker への応用: 次回巡回で新規キャストが出現していないか確認 → 一致すれば「新人入店」として通知

### 12.5 実例: #200 → #198（伏せ字「⚪︎貞」の解決）

```
#198  2026/10/24 11:01   唾をペニスに垂らす女！風上がりなのか？
#200  2026/10/24 13:15   >>198  垂らして貰えるだけありがたいと思え、⚪︎貞
```

- キャスト名: ⚪︎貞（伏せ字。末尾が「貞」）
- サービス情報: 唾をペニスに垂らす
- 伏せ字解決: キャスト辞書から「貞」で終わる名前を検索 → 候補が1つに絞れれば「解決」

### 12.6 実例: #172 → #182（キャスト「吉川」の特徴抽出）

```
#172  2026/10/21 16:16   前入った吉川さんめちゃ美人やった。最高
#182  2026/10/22 14:19   >>172  スタイルは良いな。顔は好みあると思うけどとにかく寛容
```

- キャスト名: 吉川
- 特徴: 美人 / スタイル良い / 寛容
- estama-tracker への応用: 吉川の estama 完売率と口コミスコアを突合

### 12.7 実例: #177 → #178（キャスト「姫野」の移籍/引退）

```
#177  2026/10/22 00:07   姫野って辞めた？
#178  2026/10/22 00:47   >>177  だいぶ前に辞めてる
```

- キャスト名: 姫野
- シグナル種別: 引退/移籍
- estama-tracker への応用: estama の cast_schedule で出勤消失を確認 → 一致すれば `cast_movement_candidates` に記録

### 12.8 追加テーブル

```sql
-- キャスト移籍/引退候補
CREATE TABLE cast_movement_candidates (
  cast_name   TEXT,
  shop_id     INTEGER,
  signal_type TEXT,   -- 'moved' | 'retired' | 'new_hire'
  source_post TEXT,   -- thread_id:post_no
  detected_at TEXT,
  confirmed   INTEGER DEFAULT 0
);

-- キャスト別バズタイムライン（1時間単位）
CREATE TABLE cast_buzz_timeline (
  cast_id       TEXT,
  hour_bucket   TEXT,
  mention_count INTEGER,
  PRIMARY KEY (cast_id, hour_bucket)
);

-- 店舗バズスコア
CREATE TABLE shop_buzz_scores (
  shop_id        INTEGER,
  score_date     TEXT,
  view_count     INTEGER,
  reply_count    INTEGER,
  reply_velocity REAL,
  overhyped      REAL,
  PRIMARY KEY (shop_id, score_date)
);
```

### 12.9 推奨着手順（更新版）

**Phase 1**（口コミ結合）→ **Phase 2**（爆サイ差分取得 + アンカー解析）→ **Phase 3**（LLM 特徴要約 + バズタイムライン）→ **Phase 4**（メンエスノート追加 + 移籍/引退検知）

Phase 2 でアンカー解析を組み込むのがポイント。これがないと伏せ字の解決精度が上がらない。
