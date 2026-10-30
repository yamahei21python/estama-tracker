#!/usr/bin/env python3
"""営業日 2026-10-27 の徹底的な分析。DB 非改変（最終状態は分析専用で反転）"""
import sqlite3
from pathlib import Path
from collections import Counter, defaultdict

DB = Path(__file__).with_name("estama.db")
DATE = "2026-10-27"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=" * 70)
print(f"【営業日 {DATE} 徹底的分析】")
print("=" * 70)

# ---- 1. 基本統計 ----
cur.execute("""
SELECT status_norm, COUNT(*) FROM snapshots
WHERE target_date = ? GROUP BY status_norm ORDER BY COUNT(*) DESC
""", (DATE,))
rows = cur.fetchall()
total = sum(r["COUNT(*)"] for r in rows)
print(f"\n【1. 基本統計】合計 {total} 行")
for r in rows:
    rate = r["COUNT(*)"] / total * 100 if total else 0
    print(f"   {str(r['status_norm'] or '-'):12s} {r['COUNT(*)']:>6} ({rate:5.1f}%)")

cur.execute("SELECT COUNT(DISTINCT shop_id) FROM snapshots WHERE target_date=?", (DATE,))
print(f"   対象店舗数 : {cur.fetchone()[0]} 店")
cur.execute("SELECT COUNT(DISTINCT cast_id) FROM snapshots WHERE target_date=?", (DATE,))
print(f"   対象キャスト数(cast_id): {cur.fetchone()[0]}")
cur.execute("SELECT COUNT(DISTINCT cast_name) FROM snapshots WHERE target_date=?", (DATE,))
print(f"   対象キャスト数(cast_name): {cur.fetchone()[0]}")

# ---- 2. 時系列推移（24チェックポイント全て）----
print(f"\n【2. 1日24チェックポイントの時系列推移（{DATE}）】")
cur.execute("""
SELECT fetched_time, status_norm, COUNT(*) as n
FROM snapshots WHERE target_date = ?
GROUP BY fetched_time, status_norm
ORDER BY fetched_time
""", (DATE,))
times = sorted(set(r["fetched_time"] for r in cur.fetchall()))
print(f"   チェックポイント数: {len(times)}")
# sold_out の時系列
cur.execute("""
SELECT fetched_time, COUNT(*) FROM snapshots
WHERE target_date = ? AND status_norm = 'sold_out'
GROUP BY fetched_time ORDER BY fetched_time
""", (DATE,))
so = cur.fetchall()
print(f"   sold_out の出現: {[(r['fetched_time'], r['COUNT(*)']) for r in so]}")
max_so = max(so, key=lambda r: r["COUNT(*)"]) if so else None
print(f"   完売ピーク: {max_so['fetched_time'] if max_so else '-'} ({max_so['COUNT(*)'] if max_so else 0}件)")

# ---- 3. 完売スピード分析（27日時点で完売したキャスト）----
print(f"\n【3. 完売スピード分析（{DATE}時点で完売済みのキャスト）】")
cur.execute("""
SELECT s.shop_id, s.cast_name, s.cast_id, MIN(s.fetched_time) as first_sold
FROM snapshots s
JOIN shops sh ON s.shop_id = sh.shop_id
WHERE s.target_date = ? AND s.status_norm = 'sold_out'
GROUP BY s.shop_id, s.cast_name, s.cast_id
ORDER BY first_sold
LIMIT 20
""", (DATE,))
for r in cur.fetchall():
    print(f"   {r['first_sold'][:2]}:{r['first_sold'][3:5]} | {r['cast_name']:12s} | {r['shop_id']}")

# 最初の完売までに観測したチェックポイント数を計測
cur.execute("""
WITH first_sold AS (
  SELECT shop_id, cast_name, MIN(fetched_time) AS first_sold
  FROM snapshots
  WHERE target_date = ? AND status_norm = 'sold_out'
  GROUP BY shop_id, cast_name
)
SELECT s.shop_id, s.cast_name, COUNT(*) AS checkpoints_before_sold
FROM snapshots s
JOIN first_sold f USING (shop_id, cast_name)
WHERE s.target_date = ? AND s.fetched_time <= f.first_sold
GROUP BY s.shop_id, s.cast_name
HAVING COUNT(*) > 1
ORDER BY checkpoints_before_sold
LIMIT 20
""", (DATE, DATE))
print("   複数回観測後完売（速度順）:")
for r in cur.fetchall():
    print(f"   {str(r['cast_name']):12s} | {r['checkpoints_before_sold']}回後")

# ---- 4. 普段完売なのに 27日だけ空いている（潜在アラート候補）----
print(f"\n【4. 26日が sold_out で 27日が available/closed の店/キャスト（空いている候補）】")
cur.execute("""
SELECT s1.shop_id, s1.cast_name, s1.cast_id, s1.status_norm AS d26, s2.status_norm AS d27
FROM snapshots s1
JOIN snapshots s2 ON s1.shop_id = s2.shop_id AND s1.cast_name = s2.cast_name
WHERE s1.target_date = '2026-10-26' AND s2.target_date = '2026-10-27'
  AND s1.status_norm = 'sold_out'
  AND s2.status_norm IN ('available','closed')
ORDER BY s1.cast_name
""")
rows = cur.fetchall()
print(f"   候補 {len(rows)} 件")
for r in rows[:15]:
    print(f"   {r['cast_name']:12s} | d26:{r['d26']} -> d27:{r['d27']}")

# ---- 5. cast_id vs cast_name 不一致（改名/統合候補）----
print(f"\n【5. cast_id と cast_name の不一致（改名・重複・曖昧さ）】")
cur.execute("""
SELECT cast_id, COUNT(DISTINCT cast_name) AS n_names
FROM snapshots WHERE target_date = ? AND cast_id IS NOT NULL
GROUP BY cast_id HAVING COUNT(DISTINCT cast_name) > 1
""", (DATE,))
for r in cur.fetchall():
    cur.execute("SELECT DISTINCT cast_name FROM snapshots WHERE target_date=? AND cast_id=?", (DATE, r["cast_id"]))
    names = [x["cast_name"] for x in cur.fetchall()]
    print(f"   cast_id={r['cast_id']:>7} | {r['n_names']}人の名前 → {names}")

cur.execute("""
SELECT cast_name, COUNT(DISTINCT cast_id) AS n_ids
FROM snapshots WHERE target_date = ? AND cast_id IS NOT NULL
GROUP BY cast_name HAVING COUNT(DISTINCT cast_id) > 1
""", (DATE,))
for r in cur.fetchall():
    print(f"   cast_name='{r['cast_name']}' | {r['n_ids']}人の cast_id → 同一キャストの可能性があります")

# ---- 6. work_minutes / work_hours の分布 ----
print(f"\n【6. 勤務時間分析】")
cur.execute("""
SELECT MIN(work_minutes), MAX(work_minutes), AVG(work_minutes),
       MIN(work_hours), MAX(work_hours), AVG(work_hours)
FROM snapshots WHERE target_date = ?
""", (DATE,))
r = cur.fetchone()
print(f"   work_minutes: min={r[0]}, max={r[1]}, avg={round(r[2],1)}")
print(f"   work_hours:   min={r[3]}, max={r[4]}, avg={round(r[5],2)}")

# shift 文字列と実際の勤務時間の乖離
cur.execute("""
SELECT shift_start, shift_end, MIN(work_minutes), MAX(work_minutes), AVG(work_minutes)
FROM snapshots WHERE target_date = ? AND shift_start IS NOT NULL
GROUP BY shift_start, shift_end
HAVING COUNT(*) > 5
ORDER BY shift_start, shift_end
""", (DATE,))
print("   シフト開始-終了と実勤務時間:")
for r in cur.fetchall():
    print(f"   {r['shift_start']}-{r['shift_end']} | n={r[2]}~{r[3]} (avg {round(r[4],1)})")

# ---- 7. 店舗別の完売率 ----
print(f"\n【7. 店舗別 sold_out 率トップ/ボトム（{DATE}）】")
cur.execute("""
SELECT sh.name, s.status_norm, COUNT(*) as n
FROM snapshots s
JOIN shops sh ON s.shop_id = sh.shop_id
WHERE s.target_date = ?
GROUP BY sh.shop_id, sh.name, s.status_norm
ORDER BY sh.name
""", (DATE,))
shopstats = defaultdict(lambda: Counter())
for r in cur.fetchall():
    shopstats[r["name"]][r["status_norm"]] += r["n"]
shoplist = sorted(shopstats.items(), key=lambda x: -x[1]["sold_out"] / max(x[1]["sold_out"]+x[1]["available"],1))
print("   トップ（完売率が高い店）:")
for name, c in shoplist[:10]:
    so = c["sold_out"]; avail = c["available"]; tot = so+avail
    print(f"   {name:40s} sold_out={so:>5} ({so / tot * 100 if tot else 0:.1f}%)")
print("   ボトム（空いている店）:")
for name, c in shoplist[-5:]:
    so = c["sold_out"]; avail = c["available"]; tot = so+avail
    print(f"   {name:40s} sold_out={so:>5} ({so / tot * 100 if tot else 0:.1f}%)")

# ---- 8. 全チェックポイント未観測の店/キャスト（抜け漏れ）----
print(f"\n【8. 観測抜け漏れチェック】")
cur.execute("SELECT COUNT(*) FROM shops")
n_shops = cur.fetchone()[0]
cur.execute("SELECT COUNT(DISTINCT shop_id) FROM snapshots WHERE target_date=?", (DATE,))
n_obs_shops = cur.fetchone()[0]
print(f"   shops 登録数: {n_shops}, {DATE} 観測済み店舗: {n_obs_shops} 店")
if n_shops > n_obs_shops:
    cur.execute("""
    SELECT sh.shop_id, sh.name FROM shops sh
    LEFT JOIN snapshots s ON sh.shop_id = s.shop_id AND s.target_date = ?
    WHERE s.snapshot_id IS NULL
    """, (DATE,))
    print("   未観測店舗:")
    for r in cur.fetchall():
        print(f"   {r['shop_id']} | {r['name']}")

# 各店が観測したチェックポイント数
cur.execute("""
SELECT COUNT(DISTINCT fetched_time) AS points, COUNT(*) FROM snapshots WHERE target_date=?
GROUP BY shop_id HAVING COUNT(DISTINCT fetched_time) < 24
ORDER BY COUNT(DISTINCT fetched_time)
""", (DATE,))
rows = cur.fetchall()
print(f"   24チェックポイント未満の店舗: {len(rows)} 店")
for r in rows[:10]:
    print(f"   観測点: {r['points']}/{24} (店舗数 {r['COUNT(*)']})")

# ---- 9. slot_pattern のバリエーション ----
print(f"\n【9. slot_pattern バリエーション】")
cur.execute("""
SELECT slot_pattern, COUNT(*) FROM snapshots WHERE target_date = ?
GROUP BY slot_pattern ORDER BY COUNT(*) DESC
""", (DATE,))
for r in cur.fetchall():
    print(f"   {r['slot_pattern'][:50]:50s} {r['COUNT(*)']}")

# ---- 10. 最終状態が closed だが non-closed があったキャスト（R3 反転対象）----
print(f"\n【10. 最終観測 closed だが、非 closed 観測歴あり（R3 反転で open にすべきキャスト）】")
cur.execute("""
SELECT cast_name, COUNT(*) AS total, SUM(CASE WHEN status_norm='closed' THEN 1 ELSE 0 END) AS closed_n
FROM snapshots WHERE target_date = ?
GROUP BY cast_name
HAVING SUM(CASE WHEN status_norm IN ('available','sold_out','low') THEN 1 ELSE 0 END) > 0
ORDER BY total DESC
""", (DATE,))
rows = cur.fetchall()
print(f"   {len(rows)} 件")
for r in rows[:10]:
    print(f"   {r['cast_name']:12s} | total={r['total']} closed={r['closed_n']}")

# ---- 11. fetched_time の重複（二重記録リスク）----
print(f"\n【11. fetched_time 重複チェック（UNIQUE 違反リスク）】")
cur.execute("""
SELECT shop_id, cast_name, fetched_time, COUNT(*)
FROM snapshots WHERE target_date = ?
GROUP BY shop_id, cast_name, target_date, fetched_time
HAVING COUNT(*) > 1
""", (DATE,))
dups = cur.fetchall()
print(f"   重複 {len(dups)} 組")
for r in dups[:10]:
    print(f"   {r['shop_id']} | {r['cast_name']} | {r['fetched_time']} ({r['COUNT(*)']}回)")

# ---- 12. cast_rankings との照合 ----
print(f"\n【12. cast_rankings の更新状況（最新 fetched_date）】")
cur.execute("SELECT DISTINCT fetched_date FROM cast_rankings ORDER BY fetched_date DESC")
dates = [r["fetched_date"] for r in cur.fetchall()]
print(f"   ランキング取得日: {dates}")
cur.execute("""
SELECT period_type, COUNT(DISTINCT area_code) AS areas, COUNT(*) as rows
FROM cast_rankings GROUP BY period_type
""")
for r in cur.fetchall():
    print(f"   {r['period_type']}: {r['areas']}エリア {r['rows']}行")

# ---- 13. 未処理の unknown / dummy ----
print(f"\n【13. unknown / dummy の内訳】")
cur.execute("""
SELECT status_norm, COUNT(*), GROUP_CONCAT(slot_pattern, '|')
FROM snapshots WHERE target_date = ? AND status_norm IN ('unknown','dummy')
GROUP BY status_norm
""", (DATE,))
for r in cur.fetchall():
    print(f"   {r['status_norm']}: {r['COUNT(*)']}件 / slot_patterns={r[2]}")

print("\n" + "=" * 70)
conn.close()
