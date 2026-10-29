import sqlite3
c = sqlite3.connect("estama.db").cursor()
print("=== ranking 存在で全期間 snapshots にない cast_id の傾向 ===")
c.execute("""
SELECT cr.age, COUNT(DISTINCT cr.cast_id)
FROM cast_rankings cr
WHERE NOT EXISTS (SELECT 1 FROM snapshots s WHERE s.cast_id = cr.cast_id)
GROUP BY cr.age
ORDER BY cr.age""")
for r in c.fetchall(): print(f"  ranking age={r[0]}: {r[1]}人（未出現）")
c.execute("""
SELECT sh.name, COUNT(DISTINCT cr.cast_id) AS n
FROM cast_rankings cr JOIN shops sh ON cr.shop_id = sh.shop_id
WHERE NOT EXISTS (SELECT 1 FROM snapshots s WHERE s.cast_id = cr.cast_id)
GROUP BY cr.shop_id ORDER BY n DESC""")
print("  店舗別 ranking-only:")
for r in c.fetchall(): print(f"  {r[0][:38]:38s} {r[1]}人")
c.execute("""
SELECT period_type, COUNT(DISTINCT cast_id)
FROM cast_rankings
WHERE NOT EXISTS (SELECT 1 FROM snapshots s WHERE s.cast_id = cast_rankings.cast_id)
GROUP BY period_type""")
for r in c.fetchall(): print(f"  period_type={r[0]}: {r[1]}人")
c.execute("""
SELECT area_name, COUNT(DISTINCT cast_id)
FROM cast_rankings
WHERE NOT EXISTS (SELECT 1 FROM snapshots s WHERE s.cast_id = cast_rankings.cast_id)
GROUP BY area_code""")
for r in c.fetchall(): print(f"  area={r[0][:25]:25s} {r[1]}人")
c.execute("""
SELECT COUNT(DISTINCT s.cast_id)
FROM snapshots s WHERE NOT EXISTS (SELECT 1 FROM cast_rankings cr WHERE cr.cast_id = s.cast_id)""")
print("  snapshots にあるが ranking にない:", c.fetchone()[0], "人")
c.execute("""
SELECT s.cast_id, s.cast_name, sh.name
FROM snapshots s JOIN shops sh ON s.shop_id=sh.shop_id
WHERE s.target_date='2026-10-27' AND s.status_norm='sold_out'
AND EXISTS (SELECT 1 FROM cast_rankings cr WHERE cr.cast_id=s.cast_id)""")
print("  27日 sold_outで ranking あり:")
for r in c.fetchall(): print(f"  {r[1]:12} | {r[2]}")
c.execute("""
SELECT s.cast_id, s.cast_name, sh.name
FROM snapshots s JOIN shops sh ON s.shop_id=sh.shop_id
WHERE s.target_date='2026-10-27' AND s.status_norm='sold_out'
AND NOT EXISTS (SELECT 1 FROM cast_rankings cr WHERE cr.cast_id=s.cast_id)""")
print("  27日 sold_outで ranking なし:")
for r in c.fetchall(): print(f"  {r[1]:12} | {r[2]}")
c.execute("""
SELECT cr.cast_name, cr.shop_name, cr.rank, s.status_norm
FROM cast_rankings cr
LEFT JOIN snapshots s ON cr.cast_id = s.cast_id AND s.target_date='2026-10-27'
WHERE cr.rank <= 3 AND cr.period_type='weekly'
GROUP BY cr.cast_id""")
print("  rank1-3 の 27日状態:")
for r in c.fetchall(): print(f"  rank{r[2]} {r[0]:12} | {r[1][:32]:32s} | {r[3] or '-'}")
# cast_id は同じだが shop が異なるケース
c.execute("""
SELECT cr.cast_id, cr.cast_name, cr.shop_id AS r_shop, s.shop_id AS s_shop
FROM cast_rankings cr
JOIN snapshots s ON s.cast_id = cr.cast_id AND s.target_date='2026-10-27'
WHERE cr.shop_id != s.shop_id
GROUP BY cr.cast_id
""")
print("\n cast_id は同じだが shop_id が異なる:")
for r in c.fetchall():
    print(f"  cast_id={r[0]} | ranking={r[2]} vs snapshots={r[3]} | {r[1]}")
c.execute("""
SELECT COUNT(DISTINCT cr.cast_id)
FROM cast_rankings cr
JOIN snapshots s ON s.cast_id = cr.cast_id AND s.target_date='2026-10-27'
WHERE cr.shop_id = s.shop_id
""")
print("\n ranking と snapshots で cast_id+shop_id 両方一致:", c.fetchone()[0])
# ranking の shop_id 共通数
c.execute("""
SELECT COUNT(*) FROM (
  SELECT DISTINCT cr.shop_id FROM cast_rankings
  INTERSECT
  SELECT DISTINCT s.shop_id FROM snapshots
)""")
print("ranking × snapshots 共通 shop_id:", c.fetchone()[0])
