"""Tests for daily_cast_status rollup + report sections.

Guards the alert-history foundation: the rollup must reproduce the R3
effective final state (incl. closed walk-back) one row per cast-day,
be idempotent, and key by cast_id with name fallback.
Run: python3 tests/test_rollup.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from estama_tracker import (  # noqa: E402
    rollup_daily, section_daily, section_streaks,
    section_transitions, section_consistency,
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


def add(conn, cast, date, norm, fetched_at, cast_id=None):
    conn.execute(
        "INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,"
        " status_norm, target_date, fetched_at)"
        " VALUES (1, ?, ?, ?, ?, ?, ?)",
        (cast, cast_id, norm, norm, date, fetched_at),
    )


def roll(conn):
    return conn.execute(
        "SELECT shop_id, cast_key, cast_id, cast_name, target_date,"
        " status_norm, fetched_at FROM daily_cast_status").fetchall()


# 1. rollup materializes R3 final states (closed tail walks back)
c = fresh()
add(c, "A", "2026-10-26", "sold_out", "2026-10-26 12:00:00 JST", cast_id=10)
add(c, "A", "2026-10-26", "closed", "2026-10-27 05:31:00 JST", cast_id=10)
add(c, "B", "2026-10-26", "available", "2026-10-26 12:00:00 JST")
rollup_daily(c)
rows = roll(c)
check("rollup: one row per cast-day", len(rows) == 2)
a = [r for r in rows if r[3] == "A"][0]
check("rollup: R3 walk-back keeps sold_out", a[5] == "sold_out")
check("rollup: cast_key uses cast_id", a[1] == "10")
b = [r for r in rows if r[3] == "B"][0]
check("rollup: name fallback key", b[1] == "B" and b[2] is None)

# 2. idempotent: second rollup writes the same rows
rollup_daily(c)
check("rollup: idempotent", len(roll(c)) == 2)

# 3. per-date rollup only rebuilds that date
add(c, "A", "2026-10-27", "available", "2026-10-27 12:00:00 JST", cast_id=10)
rollup_daily(c, "2026-10-27")
rows = roll(c)
check("rollup: per-date adds only that day",
      len(rows) == 3 and sum(r[4] == "2026-10-27" for r in rows) == 1)

# 4. rename merges under one cast_key
c = fresh()
add(c, "旧名", "2026-10-26", "sold_out", "2026-10-26 12:00:00 JST", cast_id=55)
add(c, "新名", "2026-10-26", "available", "2026-10-26 18:00:00 JST", cast_id=55)
rollup_daily(c)
rows = roll(c)
check("rollup: rename = 1 row", len(rows) == 1 and rows[0][1] == "55")

# 5. section_daily counts use R3 states
c = fresh()
add(c, "A", "2026-10-26", "sold_out", "2026-10-26 12:00:00 JST", cast_id=1)
add(c, "A", "2026-10-26", "closed", "2026-10-27 05:31:00 JST", cast_id=1)
lines = section_daily(c, ["2026-10-26"])
check("section_daily: sold_out counted", "sold_out=  1" in lines[0])

# 6. streaks across days
c = fresh()
for d in ("2026-10-26", "2026-10-27", "2026-10-28"):
    add(c, "S", d, "sold_out", f"{d} 20:00:00 JST", cast_id=7)
add(c, "S", "2026-10-29", "available", "2026-10-29 12:00:00 JST", cast_id=7)
lines = section_streaks(c, ["2026-10-26", "2026-10-27",
                            "2026-10-28", "2026-10-29"], streak_min=2)
check("section_streaks: streak ends at latest day",
      lines == [] or all("S (" not in l for l in lines))
# now make 10-29 sold out too -> 4-day streak
c.execute("UPDATE snapshots SET status_norm='sold_out'"
          " WHERE target_date='2026-10-29'")
lines = section_streaks(c, ["2026-10-26", "2026-10-27",
                            "2026-10-28", "2026-10-29"], streak_min=2)
check("section_streaks: 4-day streak found",
      len(lines) == 1 and "4日連続完売" in lines[0])

# 7. transitions between last two days
c = fresh()
add(c, "A", "2026-10-26", "sold_out", "2026-10-26 20:00:00 JST", cast_id=1)
add(c, "A", "2026-10-27", "available", "2026-10-27 12:00:00 JST", cast_id=1)
add(c, "B", "2026-10-27", "available", "2026-10-27 12:00:00 JST", cast_id=2)
lines = section_transitions(c, ["2026-10-26", "2026-10-27"])
check("section_transitions: churn + matrix",
      any("共通=1" in l and "新規=1" in l for l in lines)
      and any("sold_out   -> available" in l for l in lines))

# 8. consistency: rename detection
c = fresh()
add(c, "旧名", "2026-10-26", "sold_out", "2026-10-26 12:00:00 JST", cast_id=55)
add(c, "新名", "2026-10-26", "available", "2026-10-26 18:00:00 JST", cast_id=55)
lines = section_consistency(c)
check("section_consistency: rename detected",
      any("1組" in l for l in lines))

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
