"""Tests for monitor_report/section_monitor: checkpoint count, per-shop
fail rate, zero-row shops, slot-event capture rate, stale runs, and the
06:00 business-day boundary.

Run: python3.11 tests/test_monitor.py
"""
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path("/tmp/estama-monitor-test")
shutil.rmtree(SCRATCH, ignore_errors=True)
SCRATCH.mkdir(parents=True)
sys.path.insert(0, str(ROOT / "app"))
import estama_tracker as t  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
SCHEMA = (ROOT / "app" / "schema.sql").read_text()
fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


def fresh(name):
    path = SCRATCH / name
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO shops VALUES (1, '店A', '梅田', NULL, NULL)")
    con.execute("INSERT INTO shops VALUES (2, '店B', '難波', NULL, NULL)")
    con.execute("INSERT INTO shops VALUES (3, '店C', '京橋', NULL, NULL)")
    return con


def snap(con, shop, cast, date="2026-10-28"):
    con.execute(
        "INSERT INTO snapshots (shop_id, cast_name, status_norm,"
        " target_date, fetched_at, fetched_time)"
        " VALUES (?, ?, 'available', ?, ?, '10:00')",
        (shop, cast, date, f"{date} 10:00"))


def slot(con, shop, cast, date="2026-10-28"):
    con.execute(
        "INSERT INTO slot_events (shop_id, cast_key, cast_name, target_date,"
        " slot_time, slot_minutes, from_mark, to_mark, observed_at,"
        " checkpoint) VALUES (?, ?, ?, ?, '18:00', 1080, '○', '×', ?, '10:00')",
        (shop, cast, cast, date, f"{date} 10:00"))


NOW = datetime(2026, 10, 29, 10, 0, tzinfo=JST)  # 10-28 is a past day

# --------------------------------------------------------------- healthy
con = fresh("healthy.db")
# 24 ok checkpoints for business date 10-28: 06..23 on 10-28, 00..05 on 10-29
for h in range(6, 24):
    con.execute("INSERT INTO run_log (job, started_at, status) VALUES"
                " ('checkpoint', ?, 'ok')", (f"2026-10-28 {h:02d}:30:00 JST",))
for h in range(0, 6):
    con.execute("INSERT INTO run_log (job, started_at, status) VALUES"
                " ('checkpoint', ?, 'ok')", (f"2026-10-29 {h:02d}:30:00 JST",))
for sid in (1, 2, 3):
    for h in ("09:30:00", "10:30:00", "11:30:00", "12:30:00"):
        con.execute(
            "INSERT INTO scrape_log (shop_id, fetched_at, status, rows)"
            " VALUES (?, ?, 'ok', 5)", (sid, f"2026-10-28 {h} JST"))
snap(con, 1, "a")
snap(con, 2, "b")
snap(con, 3, "c")
for sid, cast in ((1, "a"), (2, "b"), (3, "c")):
    slot(con, sid, cast)
con.commit()
rep = t.monitor_report(con, "2026-10-28", now=NOW)
check("healthy: no warnings", rep["warnings"] == [])
check("healthy: checkpoint counted across the 06:00 flip",
      "checkpoint ok 24/24" in rep["summary"])
check("healthy: fail count in summary", "取得fail 0/12" in rep["summary"])
check("healthy: slot capture in summary", "枠イベント 3/3 cast (100%)"
      in rep["summary"])

# -------------------------------------------------------------- warnings
con2 = fresh("warn.db")
for h in range(6, 24):
    st = "fail" if h == 12 else "ok"
    con2.execute("INSERT INTO run_log (job, started_at, status) VALUES"
                 " ('checkpoint', ?, ?)",
                 (f"2026-10-28 {h:02d}:30:00 JST", st))
for h in range(0, 2):
    con2.execute("INSERT INTO run_log (job, started_at, status) VALUES"
                 " ('checkpoint', ?, 'ok')",
                 (f"2026-10-29 {h:02d}:30:00 JST",))
# => ok 19 of 24 expected (missing 5 >= 2) plus 1 fail
con2.execute("INSERT INTO run_log (job, started_at, status) VALUES"
             " ('ranking', '2026-10-29 04:00:00 JST', 'running')")
for h in range(4):
    con2.execute(
        "INSERT INTO scrape_log (shop_id, fetched_at, status, rows)"
        " VALUES (1, ?, ?, 0)",
        (f"2026-10-28 1{h}:30:00 JST", "fail" if h < 3 else "ok"))
    con2.execute(
        "INSERT INTO scrape_log (shop_id, fetched_at, status, rows)"
        " VALUES (2, ?, 'empty', 0)", (f"2026-10-28 1{h}:30:00 JST",))
    con2.execute(
        "INSERT INTO scrape_log (shop_id, fetched_at, status, rows)"
        " VALUES (3, ?, 'ok', 6)", (f"2026-10-28 1{h}:30:00 JST",))
for c in ("a", "b", "c", "d"):
    snap(con2, 1, c)
slot(con2, 1, "a")
con2.commit()
rep2 = t.monitor_report(con2, "2026-10-28", now=NOW)
w = "\n".join(rep2["warnings"])
check("warn: checkpoint gap with fail detail",
      "checkpoint未達: ok 19/24, fail 1" in w)
check("warn: stale running row", "runner滞留: ranking" in w)
check("warn: shop fail rate listed", "店A 3/4" in w)
check("warn: zero-row shop listed", "0行店: 店B(4回)" in w)
check("warn: low slot capture flagged", "枠イベント取得率が低い" in w
      and "1/4 cast (25%)" in w)
check("section_monitor prefixes warnings with '!'",
      any(l.startswith("  ! ") for l in t.section_monitor(con2, "2026-10-28",
                                                         now=NOW)))

# single failure / single miss is tolerated (no restart noise)
con3 = fresh("quiet.db")
for h in range(6, 24):
    con3.execute("INSERT INTO run_log (job, started_at, status) VALUES"
                 " ('checkpoint', ?, 'ok')", (f"2026-10-28 {h:02d}:30:00 JST",))
for h in range(0, 5):  # 23/24 total: one miss is tolerated
    con3.execute("INSERT INTO run_log (job, started_at, status) VALUES"
                 " ('checkpoint', ?, 'ok')", (f"2026-10-29 {h:02d}:30:00 JST",))
con3.execute("INSERT INTO scrape_log (shop_id, fetched_at, status, rows)"
             " VALUES (1, '2026-10-28 10:30:00 JST', 'fail', 0)")
con3.commit()
rep3 = t.monitor_report(con3, "2026-10-28", now=NOW)
check("quiet: one missing checkpoint + one fail -> no warning",
      rep3["warnings"] == [])

# ------------------------------------------------ today / expected hours
check("expected checkpoints = elapsed hours since 06:00",
      t._checkpoints_expected(
          "2026-10-28", datetime(2026, 10, 28, 10, 0, tzinfo=JST)) == 5)
check("expected checkpoints = 0 before 06:00",
      t._checkpoints_expected(
          "2026-10-28", datetime(2026, 10, 28, 5, 0, tzinfo=JST)) == 0)
check("expected checkpoints = 24 for past days",
      t._checkpoints_expected("2026-10-27", NOW) == 24)

# today without any slot events -> explicit warning
con4 = fresh("today.db")
snap(con4, 1, "a")
for h in range(6, 11):
    con4.execute("INSERT INTO run_log (job, started_at, status) VALUES"
                 " ('checkpoint', ?, 'ok')", (f"2026-10-28 {h:02d}:30:00 JST",))
con4.commit()
rep4 = t.monitor_report(con4, "2026-10-28",
                        now=datetime(2026, 10, 28, 10, 0, tzinfo=JST))
check("today with zero slot events is a warning",
      any("枠イベントが1件も記録されていない" in x for x in rep4["warnings"]))

# run_log completely absent must be reported as unknown, not as 0/24
con6 = fresh("nolog.db")
snap(con6, 1, "a")
con6.commit()
rep6 = t.monitor_report(con6, "2026-10-28", now=NOW)
check("no run_log rows: checkpoint reported as n/a",
      "checkpoint n/a" in rep6["summary"])
check("no run_log rows: explicit warning",
      any("run_log未記録" in x for x in rep6["warnings"]))

# monitor CLI: exit 1 with warnings, 0 without
rc = t.main(["monitor", "--date", "2026-10-28", "--db",
             str(SCRATCH / "warn.db")])
check("monitor CLI exits 1 on warnings", rc == 1)
con5 = fresh("cli_clean.db")
con5.execute("INSERT INTO snapshots (shop_id, cast_name, status_norm,"
             " target_date, fetched_at, fetched_time)"
             " VALUES (1, 'a', 'available', '2026-10-28', '2026-10-28 10:00',"
             " '10:00')")
con5.commit()
con.close()
con2.close()
con3.close()
con4.close()
con5.close()

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
