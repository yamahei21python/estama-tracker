#!/usr/bin/env python3
"""営業日 2026-10-27 の追加分析（クロス集約・傾向抽出）"""
import sqlite3
from pathlib import Path
from collections import Counter, defaultdict

DB = Path(__file__).with_name("estama.db")
DATE = "2026-10-27"
conn = sqlite3.connect(DB)
c = conn.cursor()

def q(sql, *p):
    c.execute(sql, p)
    return c.fetchall()

print("=" * 72)
print("【追加分析 1/7】time_series_推移（1時間おき、全ステータス）")
print("=" * 72)
rows = q("""
SELECT fetched_time, status_norm, COUNT(*) FROM snapshots
WHERE target_date = ? GROUP BY fetched_time, status_norm
ORDER BY fetched_time, status_norm
""", DATE)
headers = ["fetched_time"] + sorted(set(r[1] for r in rows))
print("   " + " | ".join(f"{h:>8}" for h in headers))
for t in sorted(set(r[0] for r in rows)):
    cnt = {r[1]: r[2] for r in rows if r[0] == t}
    print("   " + " | ".join(f"{cnt.get(h, 0):>8}" for h in headers))

print("\n" + "=" * 72)
print("【追加分析 2/7】完了直前 30 分の時間窓（完売直前の availability 低下）")
print("=" * 72)
# sold_out 直前30分に available が何回観測されたか
cur = q("""
SELECT s.shop_id, s.cast_name, s.cast_id, s.fetched_time as sold_at,
  COUNT(*) FILTER (WHERE s2.status_norm='available') as seen_available_before
FROM snapshots s
JOIN snapshots s2 ON s.shop_id = s2.shop_id AND s.cast_name = s2.cast_name
  AND s2.target_date = s.target_date AND s2.fetched_time < s.fetched_time
  AND datetime('2000-01-01 ' || s2.fetched_time) >= datetime('2000-01-01 ' || s.fetched_time, '-30 minutes')
  AND s2.target_date = '2026-10-27'
WHERE s.target_date = '2026-10-27' AND s.status_norm = 'sold_out'
GROUP BY s.snapshot_id
ORDER BY s.fetched_time
""",)
for r in cur:
    print(f"   {r[3][:5]} | {r[1]:12} | sold_out 前 {r[4]}回 available 観測")

print("\n" + "=" * 72)
print("【追加分析 3/7】area（店舗地域）別 sold_out 率")
print("=" * 72)
shop_area = {r[0]: r[2] for r in q("SELECT shop_id, name, area FROM shops")}
stats = defaultdict(lambda: {"so": 0, "base": 0, "so_c" : set(), "names": set()})
for r in q("""
SELECT shop_id, status_norm FROM snapshots WHERE target_date = ?
""", DATE):
    area = shop_area.get(r[0], "?")
    stats[area]["names"].add(r[0])
    if r[1] == "sold_out":
        stats[area]["so"] += 1; stats[area]["so_c"].add(r[0])
    if r[1] in ("available", "closed", "low"):
        stats[area]["base"] += 1
for area in sorted(stats):
    s = stats[area]
    if s["base"] == 0: continue
    print(f"   {area[:30]:30s} sold_out={s['so']:>3} ({s['so']/s['base']*100:5.1f}%) {len(s['names'])}店")
# area が不明な店
unc = q("SELECT name, area FROM shops WHERE area IS NULL")
if unc: print(f"   area未設定店舗: {[x[0] for x in unc]}")

print("\n" + "=" * 72)
print("【追加分析 4/7】cast_rankings 上位 10 位との一致率（ランキング = 人気＝完売？）")
print("=" * 72)
# 最新ランキングだけを対象にする
latest_row = q("SELECT MAX(fetched_date) FROM cast_rankings")
latest = latest_row[0][0] if latest_row and latest_row[0][0] else None
rank10 = {r[0] for r in q("""
    SELECT cast_id FROM cast_rankings
    WHERE period_type='weekly' AND rank <= 10 AND fetched_date = ?
""", latest)} if latest else set()
cast_status = q("""
SELECT cast_id, status_norm FROM snapshots WHERE target_date = ? AND cast_id IS NOT NULL
""", DATE)
ranked = {r[0]: r[1] for r in cast_status if r[0] in rank10}
unranked = {r[0]: r[1] for r in cast_status if r[0] not in rank10}
def so_rate(d):
    so = sum(1 for v in d.values() if v == "sold_out"); base = len(d)
    return so/base if base else 0
print(f"   ranking 上位 10 のキャスト: {len(ranked)}人 (latest ranking: {latest})")
print(f"   sold_out 率  [ranking 上位: {so_rate(ranked)*100:.1f}%]  vs  [それ以外: {so_rate(unranked)*100:.1f}%]")
# 上位 10 の詳細
top = {}
for r in q("""
SELECT cast_name, shop_name, rank, cast_id FROM cast_rankings
WHERE period_type='weekly' AND rank <= 10 AND fetched_date = ?
GROUP BY cast_id ORDER BY rank
""", latest):
    top[r[3]] = (r[0], r[1], r[2])
for rid,(name,shop,rank) in top.items():
    st = ranked.get(rid,"?")
    print(f"   rank{rank} {name:12} ({shop})  -> 27日の状態: {st}")

print("\n" + "=" * 72)
print("【追加分析 5/7】シフト開始時間（parse_clock 変換）別 sold_out 率")
print("=" * 72)
def parse_clock(t):
    """'9:00' -> 540分、『29:00』-> 1740 分（同形）"""
    if not t: return None
    try: h, m = t.split(":"); return int(h)*60 + int(m)
    except (TypeError, ValueError): return None
shift_stats = defaultdict(lambda: {"so":0, "base":0})
for r in q("""
SELECT shift_start, status_norm FROM snapshots WHERE target_date = ? AND shift_start IS NOT NULL
""", DATE):
    parsed = parse_clock(r[0])
    k = parsed if parsed is not None else -1
    if r[1] == "sold_out": shift_stats[k]["so"] += 1
    if r[1] in ("available","closed","low"): shift_stats[k]["base"] += 1
for k in sorted(shift_stats):
    s = shift_stats[k]
    hh = f"{k//60}:{k%60:02d}"
    print(f"   {hh:>6} 開始 | sold_out={s['so']:>3} ({s['so']/s['base']*100 if s['base'] else 0:5.1f}%)")

print("\n" + "=" * 72)
print("【追加分析 6/7】年齢×status のクロス集計（売れ筋年齢層）")
print("=" * 72)
age_stats = defaultdict(lambda: {"so":0, "total":0})
for r in q("""
SELECT age, status_norm FROM snapshots WHERE target_date = ? AND age IS NOT NULL
""", DATE):
    age_stats[r[0]]["total"] += 1
    if r[1] == "sold_out": age_stats[r[0]]["so"] += 1
for age in sorted(age_stats):
    s = age_stats[age]
    print(f"   {age:3}歳 | n={s['total']:>4} | sold_out={s['so']:>2} ({s['so']/s['total']*100 if s['total'] else 0:5.1f}%)")

print("\n" + "=" * 72)
print("【追加分析 7/7】店タイプ別（表記から抽出）: マンション型/派遣型/出張型/其他")
print("=" * 72)
type_map = {"マンション型": "マンション", "派遣": "派遣", "出張": "出張",
            "ルーム": "ルーム", "総合": "総合"}
type_stats = defaultdict(lambda: {"so":0, "base":0, "names": set()})
for r in q("""
SELECT sh.name, s.status_norm FROM snapshots s JOIN shops sh ON s.shop_id=sh.shop_id
WHERE s.target_date = ?
""", DATE):
    name = r[0].lower()
    typ = "其他"
    for k, v in type_map.items():
        if k.lower() in name: typ = v; break
    type_stats[typ]["names"].add(r[0])
    if r[1] == "sold_out": type_stats[typ]["so"] += 1
    if r[1] in ("available","closed","low"): type_stats[typ]["base"] += 1
for typ in sorted(type_stats):
    s = type_stats[typ]
    print(f"   {typ[:10]:10s} sold_out={s['so']:>3} ({s['so']/s['base']*100 if s['base'] else 0:5.1f}%) {len(s['names'])}店")

print("\n" + "=" * 72)
print("【追加分析 8/7-2】status 安定度（10/26 vs 10/27 の状態変化マトリクス）")
print("=" * 72)
m = defaultdict(int)
for r in q("""
WITH latest AS (
  SELECT shop_id, cast_name, target_date, MAX(fetched_time) AS fetched_time
  FROM snapshots
  WHERE target_date IN ('2026-10-26', '2026-10-27')
  GROUP BY shop_id, cast_name, target_date
)
SELECT s1.status_norm, s2.status_norm
FROM latest l1
JOIN snapshots s1 ON s1.shop_id=l1.shop_id AND s1.cast_name=l1.cast_name
  AND s1.target_date=l1.target_date AND s1.fetched_time=l1.fetched_time
JOIN latest l2 ON l2.shop_id=l1.shop_id AND l2.cast_name=l1.cast_name
  AND l2.target_date='2026-10-27'
JOIN snapshots s2 ON s2.shop_id=l2.shop_id AND s2.cast_name=l2.cast_name
  AND s2.target_date=l2.target_date AND s2.fetched_time=l2.fetched_time
WHERE l1.target_date='2026-10-26'
"""):
    m[r[0], r[1]] += 1
rows = sorted(m, key=lambda x: -m[x])
for k in rows[:25]:
    print(f"   {k[0]:10} -> {k[1]:10}  {m[k]:>5}件")

print("\n" + "=" * 72)
print("【追加分析 9】最終観測（27日）での sold_out キャストの全時系列")
print("=" * 72)
for r in q("""
SELECT s.cast_name, s.shop_id, sh.name, s.fetched_time, s.status_norm, s.slot_pattern
FROM snapshots s JOIN shops sh ON s.shop_id=sh.shop_id
WHERE s.target_date='2026-10-27' AND s.status_norm='sold_out'
ORDER BY s.fetched_time
"""):
    print(f"   {r[3][:5]} | {r[1]} | {r[2]:30} | {r[0]:12} | {r[4]} | {r[5]}")

conn.close()
