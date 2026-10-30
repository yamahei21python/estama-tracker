"""Contract tests for the shared time foundations in common.py:
business_date() truth table (06:00 JST flip), business_day_start(),
chrono_key() parity with CHRONO_KEY_SQL, and the monitor's SQL
business-day predicate agreeing with business_date().

Run: python3.11 tests/test_chrono.py
"""
import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="estama-chrono-test-"))
atexit.register(shutil.rmtree, SCRATCH, True)
sys.path.insert(0, str(ROOT / "app"))

import common  # noqa: E402
import estama_tracker as t  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=JST)


# ------------------------------------------------- business_date truth table
check("05:59 belongs to the previous business day",
      common.business_date(at(2026, 10, 29, 5, 59)) == "2026-10-28")
check("06:00 belongs to the new business day",
      common.business_date(at(2026, 10, 29, 6, 0)) == "2026-10-29")
check("23:59 stays on the same business day",
      common.business_date(at(2026, 10, 29, 23, 59)) == "2026-10-29")
check("00:30 (next calendar day) stays on the business day",
      common.business_date(at(2026, 10, 30, 0, 30)) == "2026-10-29")
check("05:59 next calendar day still previous business day",
      common.business_date(at(2026, 10, 30, 5, 59)) == "2026-10-29")

# ---------------------------------------------------- business_day_start
start = common.business_day_start("2026-10-28")
check("business_day_start is 06:00 JST",
      start == at(2026, 10, 28, 6, 0) and start.tzinfo is not None)
check("business_day_start honours the caller's tz",
      common.business_day_start("2026-10-28", ZoneInfo("UTC"))
      == datetime(2026, 10, 28, 6, 0, tzinfo=ZoneInfo("UTC")))

# tracker/runner delegates agree with common (one rule, not three)
check("tracker _expected_business_date delegates",
      t._expected_business_date(at(2026, 10, 29, 5, 59)) == "2026-10-28")

# ---------------------------------------------------------- chrono_key
check("chrono_key keeps fetched_at verbatim",
      common.chrono_key("2026-10-28 10:00 JST", "10:00")
      == "2026-10-28 10:00 JST")
check("chrono_key legacy fallback is date-less and sorts last (ASC)",
      common.chrono_key(None, "24:30") == "9999-12-31 24:30")
check("chrono_key legacy fallback defaults the time",
      common.chrono_key(None, None) == "9999-12-31 00:00")

# ------------------------------------- SQL fragment matches the Python twin
con = sqlite3.connect(SCRATCH / "chrono.db")
con.executescript((ROOT / "app" / "schema.sql").read_text())
con.execute("INSERT INTO shops VALUES (1, '店A', '梅田', NULL, NULL)")
rows = [
    ("2026-10-28 10:00 JST", "10:00"),
    ("2026-10-29 00:30 JST", "00:30"),
    (None, "12:00"),
    (None, None),
    ("2026-10-28 09:00 JST", "09:00"),
]
for i, (fa, ft) in enumerate(rows):
    con.execute(
        "INSERT INTO snapshots (snapshot_id, shop_id, cast_name, status_norm,"
        " target_date, fetched_at, fetched_time)"
        " VALUES (?, 1, ?, 'available', '2026-10-28', ?, ?)",
        (i + 1, f"c{i}", fa, ft))
con.commit()
sql_keys = {sid: k for sid, k in con.execute(
    f"SELECT snapshot_id, {common.CHRONO_KEY_SQL} FROM snapshots")}
check("CHRONO_KEY_SQL equals chrono_key() row by row",
      all(sql_keys[i + 1] == common.chrono_key(fa, ft)
          for i, (fa, ft) in enumerate(rows)))
sql_order = [sid for sid, _ in con.execute(
    f"SELECT snapshot_id, {common.CHRONO_KEY_SQL} AS k"
    " FROM snapshots ORDER BY k")]
py_order = [i + 1 for i, _ in sorted(
    enumerate(rows), key=lambda p: common.chrono_key(*p[1]))]
check("CHRONO_KEY_SQL ordering equals Python ordering",
      sql_order == py_order)

# --------------------------------- monitor SQL predicate vs business_date
cases = [
    ("2026-10-28 05:59:59 JST", "2026-10-27"),
    ("2026-10-28 06:00:00 JST", "2026-10-28"),
    ("2026-10-29 00:30:00 JST", "2026-10-28"),
    ("2026-10-29 05:59:59 JST", "2026-10-28"),
    ("2026-10-29 06:00:00 JST", "2026-10-29"),
]
for fa, _ in cases:
    con.execute("INSERT INTO run_log (job, started_at, status)"
                " VALUES ('checkpoint', ?, 'ok')", (fa,))
con.commit()
pred = t._bd_cond("started_at")
for fa, expected in cases:
    n = con.execute(
        f"SELECT COUNT(*) FROM run_log WHERE started_at = ? AND {pred}",
        (fa, expected)).fetchone()[0]
    check(f"SQL business day of {fa} is {expected}", n == 1)
check("SQL predicate assigns every row to exactly one business day",
      sum(con.execute(f"SELECT COUNT(*) FROM run_log WHERE {pred}", (bd,)
                      ).fetchone()[0] for bd in {e for _, e in cases})
      == len(cases))
con.close()

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
