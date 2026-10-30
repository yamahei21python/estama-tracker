#!/usr/bin/env python3
"""DB の“穴”を可視化：NULL・未設定・取得できていないフィールド"""
import sqlite3
from pathlib import Path
from collections import Counter

DB_PATH = Path(__file__).with_name("estama.db")
TARGET_DATE = "2026-10-27"


def text(value, width=0):
    """None を含むDB値を安全に表示する。"""
    value = "-" if value is None else str(value)
    return value[:width] if width else value


conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("=" * 68)
print("【NULL/未設定フィールドの欠落率（27日）】")
print("=" * 68)
fields = [("cast_id","CAST"),("age","INT"),("shift","TEXT"),("shift_start","TIME"),
          ("shift_end","TIME"),("work_minutes","MIN"),("work_hours","HRS"),
          ("status_raw","TEXT"),("slot_pattern","TEXT"),("fetched_at","DT"),("fetched_time","TIME")]
total = c.execute("SELECT COUNT(*) FROM snapshots WHERE target_date=?", (TARGET_DATE,)).fetchone()[0]
for fld, _ in fields:
    tot = total
    nulls = c.execute(f"SELECT COUNT(*) FROM snapshots WHERE target_date=? AND {fld} IS NULL", (TARGET_DATE,)).fetchone()[0]
    rate = nulls / tot * 100 if tot else 0
    print(f"   {fld:14s} n={tot:5} NULL={nulls:5} ({rate:4.1f}%)")

print("\n" + "=" * 68)
print("【age が NULL のキャスト：何らかの情報抜けているか】")
print("=" * 68)
for r in c.execute("""
SELECT s.cast_name, s.shop_id, sh.name, s.slot_pattern, s.status_norm
FROM snapshots s JOIN shops sh ON s.shop_id=sh.shop_id
WHERE s.target_date=? AND s.age IS NULL LIMIT 20
""", (TARGET_DATE,)):
    print(f"   {text(r[0], 14):14} | {text(r[1])} | {text(r[2])} | {text(r[3], 30)}")

print("\n" + "=" * 68)
print("【slot_pattern があるが status_raw が NULL の行（正規化元がない）】")
print("=" * 68)
n = c.execute("""
SELECT COUNT(*) FROM snapshots WHERE target_date=? AND slot_pattern IS NOT NULL AND status_raw IS NULL
""", (TARGET_DATE,)).fetchone()[0]
print(f"   {n}行（slot_pattern のみ存在 → スクレイパーのどこかで別ソース）")

print("\n" + "=" * 68)
print("【age が NULL でかつ slot_pattern あり の詳細パターン】")
print("=" * 68)
cnt = Counter()
for r in c.execute("""
SELECT slot_pattern FROM snapshots WHERE target_date=? AND age IS NULL
""", (TARGET_DATE,)):
    cnt[r[0]] += 1
for k, v in cnt.most_common(15):
    print(f"   {text(k, 45):45s} {v}")

print("\n" + "=" * 68)
print("【work_minutes が NULL のケース（推定勤務時間のみ）】")
print("=" * 68)
n = c.execute("""
SELECT COUNT(*) FROM snapshots WHERE target_date=? AND work_minutes IS NULL
""", (TARGET_DATE,)).fetchone()[0]
print(f"   {n}行")
for r in c.execute("""
SELECT shift, shift_start, shift_end, work_hours FROM snapshots WHERE target_date=? AND work_minutes IS NULL LIMIT 10
""", (TARGET_DATE,)):
    print(f"   shift={r[0]} | {r[1]}-{r[2]} | work_hours={r[3]}")

print("\n" + "=" * 68)
print("【fetched_at が NULL（日付が入っていない checkpoint）】")
print("=" * 68)
n = c.execute("""
SELECT COUNT(*) FROM snapshots WHERE target_date=? AND fetched_at IS NULL
""", (TARGET_DATE,)).fetchone()[0]
print(f"   {n}行")

print("\n" + "=" * 68)
print("【fetched_time が NULL（時刻が入っていない）】")
print("=" * 68)
n = c.execute("""
SELECT COUNT(*) FROM snapshots WHERE target_date=? AND fetched_time IS NULL
""", (TARGET_DATE,)).fetchone()[0]
print(f"   {n}行")

print("\n" + "=" * 68)
print("【status_raw が NULL の行数と、それに対応する slot_pattern】")
print("=" * 68)
n = c.execute("""
SELECT COUNT(*) FROM snapshots WHERE target_date=? AND status_raw IS NULL
""", (TARGET_DATE,)).fetchone()[0]
print(f"   {n}行")
cnt = Counter()
for r in c.execute("""
SELECT slot_pattern FROM snapshots WHERE target_date=? AND status_raw IS NULL
""", (TARGET_DATE,)):
    cnt[r[0]] += 1
for k, v in cnt.most_common(10):
    print(f"   {text(k, 50):50s} {v}")

print("\n" + "=" * 68)
print("【age=UNKNOWN/年齢表記が文字列のもの】")
print("=" * 68)
for r in c.execute("""
SELECT DISTINCT age FROM snapshots WHERE target_date=? AND age IS NOT NULL AND (age < 10 OR age > 100)
""", (TARGET_DATE,)):
    print(f"   age={r[0]}")

print("\n" + "=" * 68)
print("【cast_id=0（未割当ID）】")
print("=" * 68)
n = c.execute("""
SELECT COUNT(DISTINCT cast_name) FROM snapshots WHERE target_date=? AND cast_id=0
""", (TARGET_DATE,)).fetchone()[0]
print(f"   {n}名")

print("\n" + "=" * 68)
print("【rankings に存在するが snapshots にない cast_id（逆方向）】")
print("=" * 68)
n = c.execute("""
SELECT COUNT(*) FROM cast_rankings cr
WHERE NOT EXISTS (SELECT 1 FROM snapshots s WHERE s.target_date=? AND s.cast_id=cr.cast_id)
""", (TARGET_DATE,)).fetchone()[0]
print(f"   {n}件（ranking 上位でも巡回未観測の可能性）")
for r in c.execute("""
SELECT cast_name, shop_name, rank FROM cast_rankings
WHERE NOT EXISTS (SELECT 1 FROM snapshots s WHERE s.target_date=? AND s.cast_id=cast_rankings.cast_id)
LIMIT 10
""", (TARGET_DATE,)):
    print(f"   {r[0]:12} | {r[1]} | rank{r[2]}")

print("\n" + "=" * 68)
print("【店舗の store_status 設定状況】")
print("=" * 68)
tot = c.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
ss = c.execute("SELECT store_status, COUNT(*) FROM shops GROUP BY store_status").fetchall()
print(f"   全 {tot}店:")
for r in ss: print(f"      store_status={r[0]!r} {r[1]}店")
ns = c.execute("SELECT name, source_url FROM shops WHERE source_url IS NULL").fetchall()
print(f"   source_url 未設定: {len(ns)}店")
conn.close()
