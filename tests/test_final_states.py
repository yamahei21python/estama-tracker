"""Regression tests for R3 effective final state (analysis-time only).

Guards the 2026-10-27 rule: if a cast-day's latest observation is
'closed' (end-of-shift reset / shop closing flip), the effective final
state is that day's LATEST non-closed observation; an all-closed day
stays 'closed'. The DB rows themselves must never be rewritten.
Run: python3 tests/test_final_states.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from estama_tracker import (  # noqa: E402
    final_rows, sold_out, status_counts, usually_sold_out_alert,
)

SCHEMA = (Path(__file__).resolve().parent.parent / "app" / "schema.sql").read_text()

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


def fresh():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO shops VALUES (1, 'テスト店', '梅田', NULL, NULL)")
    return conn


def add(conn, cast, date, time, norm, fetched_at=None, cast_id=None):
    """Insert one checkpoint. fetched_at format mirrors production."""
    if fetched_at is None and time:
        fetched_at = f"2026-10-26 {time}:00 JST"
    conn.execute(
        "INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,"
        " status_norm, target_date, fetched_time, fetched_at)"
        " VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
        (cast, cast_id, norm, norm, date, time, fetched_at),
    )


def finals(conn, date):
    return {r[1]: r[3] for r in final_rows(conn, date)}


# 1. end-of-day closed tail walks back to latest non-closed (sold_out kept)
c = fresh()
add(c, "A", "2026-10-26", "10:00", "available")
add(c, "A", "2026-10-26", "12:00", "sold_out")
for t in ("01:33", "02:31", "05:31"):
    add(c, "A", "2026-10-26", None, "closed",
        fetched_at=f"2026-10-27 {t}:00 JST")
check("closed tail -> walks back to sold_out", finals(c, "2026-10-26") == {"A": "sold_out"})

# 2. closed tail after available -> available (midnight ordering: last row
#    by fetched_at is the 05:31 closed, not the 23:xx closed)
c = fresh()
add(c, "B", "2026-10-26", "23:37", "available")
add(c, "B", "2026-10-26", None, "closed", fetched_at="2026-10-27 00:33:00 JST")
add(c, "B", "2026-10-26", None, "closed", fetched_at="2026-10-27 05:31:00 JST")
check("midnight: fetched_at order correct", finals(c, "2026-10-26") == {"B": "available"})

# 3. all-closed day stays closed
c = fresh()
add(c, "C", "2026-10-26", "12:00", "closed")
add(c, "C", "2026-10-26", "18:00", "closed")
check("all-closed day stays closed", finals(c, "2026-10-26") == {"C": "closed"})

# 4. non-closed latest is used as-is (no walk-back needed)
c = fresh()
add(c, "D", "2026-10-26", "12:00", "sold_out")
add(c, "D", "2026-10-26", "17:00", "low")
check("non-closed latest kept", finals(c, "2026-10-26") == {"D": "low"})

# 5. walk-back picks the LATEST non-closed (low), not the earliest (available)
c = fresh()
add(c, "E", "2026-10-26", "10:00", "available")
add(c, "E", "2026-10-26", "17:00", "low")
add(c, "E", "2026-10-26", "23:59", "closed")
check("walk-back = latest non-closed", finals(c, "2026-10-26") == {"E": "low"})

# 6. DB rows are untouched by analysis functions
c = fresh()
add(c, "F", "2026-10-26", "10:00", "available")
add(c, "F", "2026-10-26", "23:59", "closed")
status_counts(c, "2026-10-26")
sold_out(c, "2026-10-26")
raw = c.execute(
    "SELECT status_norm FROM snapshots WHERE cast_name='F' ORDER BY snapshot_id"
).fetchall()
check("analysis does not rewrite DB rows", raw == [("available",), ("closed",)])

# 7. status_counts / sold_out use effective state
c = fresh()
add(c, "G", "2026-10-26", "10:00", "sold_out")
add(c, "G", "2026-10-26", "23:59", "closed")
add(c, "H", "2026-10-26", "10:00", "available")
add(c, "H", "2026-10-26", "23:59", "closed")
check("counts use effective state",
      dict(status_counts(c, "2026-10-26")) == {"sold_out": 1, "available": 1})
so = sold_out(c, "2026-10-26")
check("sold-out list uses effective state",
      len(so) == 1 and so[0][1] == "G")

# 8. alert: prior day ended closed after sold_out still counts as sold day
c = fresh()
for d in ("2026-10-23", "2026-10-24", "2026-10-25"):
    add(c, "I", d, "12:00", "sold_out")
    add(c, "I", d, None, "closed",
        fetched_at=f"{d} 23:59:00 JST")
add(c, "I", "2026-10-26", "12:00", "available")
alerts, considered = usually_sold_out_alert(c, "2026-10-26", min_days=3)
check("alert history ignores closed reset (3/3 sold days)",
      len(alerts) == 1 and alerts[0][4] == 3 and considered == 1)

# 9. alert: target day closed-latest but effectively sold_out -> no alert
c = fresh()
for d in ("2026-10-23", "2026-10-24", "2026-10-25"):
    add(c, "J", d, "12:00", "sold_out")
add(c, "J", "2026-10-26", "12:00", "sold_out")
add(c, "J", "2026-10-26", "23:59", "closed")
alerts, _ = usually_sold_out_alert(c, "2026-10-26", min_days=3)
check("alert target uses effective state (sold_out -> no alert)",
      alerts == [])

# 10. renamed cast: one cast_id, two names -> ONE person in counts (920 rule)
c = fresh()
add(c, "旧名", "2026-10-26", "10:00", "available", cast_id=953092)
add(c, "新名", "2026-10-26", "12:00", "sold_out", cast_id=953092)
add(c, "新名", "2026-10-26", "23:59", "closed", cast_id=953092)
rows = final_rows(c, "2026-10-26")
check("rename merges into one final row (cast_id key)",
      len(rows) == 1 and rows[0][3] == "sold_out")
check("status_counts denominator = distinct cast_id",
      dict(status_counts(c, "2026-10-26")) == {"sold_out": 1})

# 11. alert survives a rename: history (old name) + target (new name)
#     on the same cast_id still match as one cast
c = fresh()
for d in ("2026-10-23", "2026-10-24", "2026-10-25"):
    add(c, "旧名", d, "12:00", "sold_out", cast_id=55)
add(c, "新名", "2026-10-26", "12:00", "available", cast_id=55)
alerts, considered = usually_sold_out_alert(c, "2026-10-26", min_days=3)
check("alert joins history/target across rename (1 cast)",
      len(alerts) == 1 and considered == 1 and alerts[0][1] == "新名")

# 12. migration: legacy '全コマ×' unknown rows are reclassified to dummy
import tempfile  # noqa: E402
from common import connect_db  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="estama-migrate-")) / "m.db"
conn = connect_db(str(tmp))
conn.execute("INSERT INTO shops VALUES (1, 'テスト店', '梅田', NULL, NULL)")
conn.execute(
    "INSERT INTO snapshots (shop_id, cast_name, status_raw, status_norm,"
    " target_date, fetched_time) VALUES (1, 'おとり',"
    " '全コマ×(事前予約不可/おとり候補)', 'unknown', '2026-10-27', '12:00')"
)
conn.commit()
conn.close()
conn = connect_db(str(tmp))  # re-open: migration runs
val = conn.execute(
    "SELECT status_norm FROM snapshots WHERE cast_name='おとり'"
).fetchone()[0]
conn.close()
check("migration reclassifies legacy 全コマ× unknown -> dummy",
      val == "dummy")

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
