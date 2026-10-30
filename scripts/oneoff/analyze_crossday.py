#!/usr/bin/env python3
"""営業日間のクロス分析：10/26 vs 10/27 の状態遷移・定常性"""
import sqlite3, sys
from pathlib import Path
from collections import Counter, defaultdict

DB = Path("estama.db")
DATE_A = "2026-10-26"; DATE_B = "2026-10-27"
conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
c = conn.cursor()

print("=" * 72)
print(f"【{DATE_A} vs {DATE_B} クロス分析】")
print("=" * 72)

# --- 1. cast 数の推移 ---
print("\n【1. キャスト数推移（R3 最終状態）】")
for date in (DATE_A, DATE_B):
    r = c.execute(f"""
        WITH final AS (
            SELECT shop_id, cast_name, cast_id, status_norm,
                   ROW_NUMBER() OVER (
                     PARTITION BY shop_id, CAST(COALESCE(cast_id, cast_name) AS TEXT)
                     ORDER BY (status_norm='closed'), COALESCE(fetched_at,'') DESC, snapshot_id DESC
                   ) AS rn
            FROM snapshots WHERE target_date = ?
        )
        SELECT status_norm, COUNT(*) FROM final WHERE rn=1 GROUP BY status_norm
    """, (date,)).fetchall()
    tot = sum(r['COUNT(*)'] for r in r)
    print(f"  {date}: 合計 {tot}")
    for rr in r: print(f"    {rr['status_norm']:10} {rr['COUNT(*)']:>5}")

# --- 2. 日次で観測されたキャスト（一致/増減） ---
print("\n【2. cast の出現/消滅（cast_key = COALESCE(cast_id,cast_name)）】")
keysA = set(k['g'] for k in c.execute("""
    SELECT CAST(COALESCE(cast_id, cast_name) AS TEXT) AS g, shop_id
    FROM snapshots WHERE target_date = ?
""", (DATE_A,)))
keysB = set(k['g'] for k in c.execute("""
    SELECT CAST(COALESCE(cast_id, cast_name) AS TEXT) AS g, shop_id
    FROM snapshots WHERE target_date = ?
""", (DATE_B,)))
print(f"  10/26 のみ: {len(keysA-keysB)} / 10/27 のみ: {len(keysB-keysA)} / 共通: {len(keysA&keysB)}")
for k in sorted(keysA-keysB)[:8]:
    r = c.execute("SELECT cast_name, s.shop_id AS s_shop, sh.name FROM snapshots s JOIN shops sh ON s.shop_id=sh.shop_id WHERE s.target_date=? AND CAST(COALESCE(s.cast_id,s.cast_name) AS TEXT)=?", (DATE_A, k)).fetchone()
    print(f"    消滅 {k} | {r['cast_name']} | {r['name']}")
for k in sorted(keysB-keysA)[:8]:
    r = c.execute("SELECT cast_name, s.shop_id AS s_shop, sh.name FROM snapshots s JOIN shops sh ON s.shop_id=sh.shop_id WHERE s.target_date=? AND CAST(COALESCE(s.cast_id,s.cast_name) AS TEXT)=?", (DATE_B, k)).fetchone()
    print(f"    出現 {k} | {r['cast_name']} | {r['name']}")

# --- 3. 10/26 sold_out → 10/27 の状態（R3 最終状態ベース） ---
print("\n【3. 10/26 sold_out キャストの 10/27 状態（R3）】")
rows = c.execute(f"""
    WITH fa AS (
        SELECT shop_id, CAST(COALESCE(cast_id, cast_name) AS TEXT) AS g, status_norm AS sA
        FROM snapshots WHERE target_date = ?
    ), fb AS (
        SELECT shop_id, CAST(COALESCE(cast_id, cast_name) AS TEXT) AS g,
               ROW_NUMBER() OVER (
                 PARTITION BY shop_id, "g"
                 ORDER BY (status_norm='closed'), COALESCE(fetched_at,'') DESC, snapshot_id DESC
               ) AS rn, status_norm AS sB
        FROM snapshots WHERE target_date = ?
    )
    SELECT fA.sA, fB.sB, COUNT(*)
    FROM fa fA JOIN fb fB ON fA."g"=fB."g" AND fA.shop_id=fB.shop_id
    WHERE fA.sA='sold_out' AND fB.rn=1
    GROUP BY fA.sA, fB.sB
    ORDER BY fB.sB
""", (DATE_A, DATE_B)).fetchall()
for r in rows: print(f"   sold_out({DATE_A}) -> {r['sB']}: {r['COUNT(*)']}件")

# --- 4. 10/26 available → 10/27 sold_out（朝完売） ---
print("\n【4. 10/26 available が 10/27 sold_out に（朝完売候補）】")
n = c.execute(f"""
    WITH fb AS (
        SELECT shop_id, CAST(COALESCE(cast_id, cast_name) AS TEXT) AS g,
               ROW_NUMBER() OVER (
                 PARTITION BY shop_id, "g"
                 ORDER BY (status_norm='closed'), COALESCE(fetched_at,'') DESC, snapshot_id DESC
               ) AS rn
        FROM snapshots WHERE target_date = ? AND status_norm='sold_out'
    )
    SELECT COUNT(DISTINCT g) FROM fb
""", (DATE_B,)).fetchone()[0]
print(f"  前日 available → 翌日 sold_out: {n}件")

# --- 5. 日次 sold_out 率の安定性（cast_key 別 sold_out 率） ---
print("\n【5. sold_out 定常性（cast_key 別）】")
# sold_out 回数の分布
dA = Counter(k for k,_ in c.execute("""
    SELECT CAST(COALESCE(cast_id,cast_name) AS TEXT), shop_id
    FROM snapshots WHERE target_date=? AND status_norm='sold_out'
""", (DATE_A,)))
dB = Counter(k for k,_ in c.execute("""
    SELECT CAST(COALESCE(cast_id,cast_name) AS TEXT), shop_id
    FROM snapshots WHERE target_date=? AND status_norm='sold_out'
""", (DATE_B,)))
print(f"  sold_out cast_key: 10/26={len(dA)}  10/27={len(dB)}")
print(f"   両日 sold_out: {len(set(dA)&set(dB))}")
# sold_out 率の相関
both = set(dA)&set(dB)
if both:
    # 両日同じ cast_key の sold_out 回数を比較
    print("   両日 sold_out の cast 数 (10/26回 -> 10/27回):")
    for a in sorted(set(dA[k] for k in both)):
        bs = sorted(dB[k] for k in both if dA[k]==a)
        print(f"     10/26={a}回: 10/27={bs}")

# --- 6. エリア別 sold_out 率の安定性 ---
print("\n【6. エリア別 sold_out 率（全 cast / available+closed+low 基準）】")
areas = {}
for r in c.execute("SELECT DISTINCT area FROM shops"):
    areas[r[0]] = {"A": {"so":0,"base":0}, "B": {"so":0,"base":0}}
for d, key in ((DATE_A,"A"),(DATE_B,"B")):
    for r in c.execute(f"""
        SELECT sh.area, s.status_norm
        FROM snapshots s JOIN shops sh ON s.shop_id=sh.shop_id
        WHERE s.target_date = ?
    """, (d,)):
        a = r['area']
        if r['status_norm'] in ("available","closed","low"):
            areas[a][key]["base"] += 1
        if r['status_norm'] == "sold_out":
            areas[a][key]["so"] += 1
rows = sorted([(a, d) for a, d in areas.items()
               if d["A"]["base"] and d["B"]["base"]],
              key=lambda x: -x[1]["B"]["so"]/x[1]["B"]["base"])
print(f"  {'エリア':25s} 10/26率   10/27率   変化")
for a, d in rows:
    ra = d["A"]["so"]/d["A"]["base"]*100
    rb = d["B"]["so"]/d["B"]["base"]*100
    print(f"  {a[:25]:25s} {ra:6.1f}%  {rb:6.1f}%  {rb-ra:+6.1f}%")

# --- 7. sold_out 店舗数の推移 ---
print("\n【7. sold_out 店舗数推移】")
for d in (DATE_A, DATE_B):
    n = c.execute(f"SELECT COUNT(DISTINCT shop_id) FROM snapshots WHERE target_date=? AND status_norm='sold_out'", (d,)).fetchone()[0]
    print(f"  {d}: {n}店")

# --- 8. area 未設定店舗 ---
print("\n【8. area 未設定店舗（ロジック未活用の実態）】")
ns = c.execute("SELECT name, store_status FROM shops WHERE area IS NULL").fetchall()
print(f"   {len(ns)}店")
for r in ns[:10]: print(f"     {r[0]} | {r[1]}")

# --- 9. sold_out の時間帯分布（両日比較） ---
print("\n【9. sold_out 時間帯分布】")
for d in (DATE_A, DATE_B):
    cnt = Counter()
    for r in c.execute(f"""
        SELECT MIN(fetched_time), MAX(fetched_time), COUNT(*)
        FROM snapshots WHERE target_date=? AND status_norm='sold_out'
        GROUP BY fetched_time
    """, (d,)):
        cnt[r[0]] = r[2]
    print(f"  {d}: {sorted(cnt.items())}")
conn.close()
