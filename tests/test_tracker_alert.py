"""Regression tests for the day-based usually_sold_out_alert.

Guards the 2026-10-26 fix: history must be counted in distinct DAYS
(a day's status = its latest observation), never in snapshot rows -
the old row-counting fired on 3 intraday checkpoints of a single day.
Run: python3 tests/test_tracker_alert.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from estama_tracker import usually_sold_out_alert  # noqa: E402

SCHEMA = (Path(__file__).resolve().parent.parent / "app" / "schema.sql").read_text()


def fresh():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO shops VALUES (1, 'テスト店', '梅田', NULL, NULL)")
    return conn


def add(conn, cast, date, time, norm):
    conn.execute(
        "INSERT INTO snapshots (shop_id, cast_name, status_raw, status_norm,"
        " target_date, fetched_time) VALUES (1, ?, ?, ?, ?, ?)",
        (cast, norm, norm, date, time),
    )


fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


# 1. three checkpoints on ONE day must not count as 3 days
c = fresh()
for t in ("09:00", "13:00", "19:00"):
    add(c, "X", "2026-10-25", t, "sold_out")
add(c, "X", "2026-10-26", "09:00", "available")
alerts, considered = usually_sold_out_alert(c, "2026-10-26", min_days=3)
check("checkpoint inflation: no alert, considered=0", alerts == [] and considered == 0)

# 2. sold out on 3 distinct days, available on target -> alert at rate 1.0
c = fresh()
for d in ("2026-10-23", "2026-10-24", "2026-10-25"):
    add(c, "Y", d, "12:00", "sold_out")
add(c, "Y", "2026-10-26", "09:00", "available")
alerts, considered = usually_sold_out_alert(c, "2026-10-26", min_days=3)
check("distinct days: Y alerts 3/3", len(alerts) == 1 and alerts[0][1] == "Y"
      and alerts[0][4] == 3 and alerts[0][5] == 3 and considered == 1)

# 3. target day: available early but sold out at the latest checkpoint -> no alert
c = fresh()
for d in ("2026-10-23", "2026-10-24", "2026-10-25"):
    add(c, "Z", d, "12:00", "sold_out")
add(c, "Z", "2026-10-26", "09:00", "available")
add(c, "Z", "2026-10-26", "15:00", "sold_out")
alerts, _ = usually_sold_out_alert(c, "2026-10-26", min_days=3)
check("latest-of-day (sold_out) suppresses alert", alerts == [])

# 4. target day: sold out early, available at the latest checkpoint -> alert
c = fresh()
for d in ("2026-10-23", "2026-10-24", "2026-10-25"):
    add(c, "W", d, "12:00", "sold_out")
add(c, "W", "2026-10-26", "09:00", "sold_out")
add(c, "W", "2026-10-26", "17:00", "available")
alerts, _ = usually_sold_out_alert(c, "2026-10-26", min_days=3)
check("latest-of-day (available) triggers alert", len(alerts) == 1 and alerts[0][1] == "W")

# 5. history day status also uses the day's latest observation
c = fresh()
add(c, "V", "2026-10-23", "09:00", "available")
add(c, "V", "2026-10-23", "18:00", "sold_out")  # day 1 ends sold out
add(c, "V", "2026-10-24", "12:00", "sold_out")
add(c, "V", "2026-10-25", "12:00", "sold_out")
add(c, "V", "2026-10-26", "09:00", "low")
alerts, _ = usually_sold_out_alert(c, "2026-10-26", min_days=3)
check("history latest-of-day: V counts 3/3 sold days", len(alerts) == 1 and alerts[0][4] == 3)

# 6. duplicates impossible: one alert row per cast even with many target rows
c = fresh()
for d in ("2026-10-23", "2026-10-24", "2026-10-25"):
    add(c, "Y", d, "12:00", "sold_out")
for t in ("09:00", "11:00", "13:00"):
    add(c, "Y", "2026-10-26", t, "available")
alerts, _ = usually_sold_out_alert(c, "2026-10-26", min_days=3)
check("no duplicate alerts", len(alerts) == 1)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
