"""Tests for DB retention (size control) + the daily-history migration.

Covers the config merge, the cutoff arithmetic (the windows are
inclusive of the newest N business dates), per-table deletion columns,
VACUUM behaviour, the archive-guard inputs, and the one thing retention
must never break: reports that answer "what happened over time" keep
reading the rollup after raw snapshots are gone.

Run: python3 tests/test_retention.py
"""
import shutil
import io
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import common  # noqa: E402
import estama_tracker as tracker  # noqa: E402
import retention  # noqa: E402
from analysis import rollup_daily  # noqa: E402
from queries import _daily_dates, _all_dates  # noqa: E402
from report_sections import section_daily, section_new_casts  # noqa: E402

SCRATCH = Path(tempfile.mkdtemp(prefix="estama-retention-test-"))
from harness import check, fails  # noqa: E402


def fresh(path):
    conn = common.connect_db(str(path))
    return conn


def add_snapshot(conn, shop, cast, cast_id, d, norm="available",
                 hhmm="10:00"):
    conn.execute(
        """INSERT OR IGNORE INTO snapshots (shop_id, cast_name, cast_id,
               status_raw, status_norm, target_date, fetched_time, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (shop, cast, cast_id, norm, norm, d, hhmm,
         f"{d} {hhmm}:00 JST"))


# ---------------------------------------------------------------- config
check("config: absent means disabled",
      retention.retention_config(None) is None)
check("config: non-dict means disabled",
      retention.retention_config("15") is None)
cfg = retention.retention_config({"snapshots_days": 15})
check("config: missing keys take defaults",
      cfg["snapshots_days"] == 15
      and cfg["daily_days"] == retention.DEFAULTS["daily_days"])
check("config: zero disables one table",
      retention.retention_config({"snapshots_days": 0})["snapshots_days"] is None)
check("config: None disables one table",
      retention.retention_config({"snapshots_days": None})["snapshots_days"]
      is None)
check("config: garbage disables the table (never delete on a typo)",
      retention.retention_config({"snapshots_days": "15d"})["snapshots_days"]
      is None)

# ---------------------------------------------------------------- cutoff
check("cutoff keeps exactly N dates",
      retention.cutoff("2026-11-05", 15) == "2026-10-22")
check("cutoff of 1 day keeps only today",
      retention.cutoff("2026-11-05", 1) == "2026-11-05")

# ------------------------------------------------------------ prune math
db = SCRATCH / "prune.db"
conn = fresh(db)
conn.execute("INSERT INTO shops VALUES (1, '店A', '梅田', NULL, NULL)")
conn.execute("INSERT INTO shops VALUES (2, '店B', '難波', NULL, NULL)")
today = "2026-11-05"
start = date.fromisoformat(today) - timedelta(days=19)
for i in range(20):                      # 20 days of snapshots
    d = (start + timedelta(days=i)).isoformat()
    add_snapshot(conn, 1, "キャストA", 100, d)
    add_snapshot(conn, 2, "キャストB", 200, d)
    conn.execute(
        """INSERT OR IGNORE INTO cast_rankings (area_code, area_name,
               period_type, rank, is_pr, cast_id, cast_name, shop_id,
               shop_name, fetched_date, fetched_at)
           VALUES ('2700', '大阪', 'weekly', 1, 0, 100, 'キャストA', 1,
                   '店A', ?, ?)""", (d, f"{d} 03:00:00 JST"))
    conn.execute(
        """INSERT INTO scrape_log (shop_id, fetched_at, status, rows)
           VALUES (1, ?, 'ok', 10)""", (f"{d} 10:00:00 JST",))
    conn.execute(
        """INSERT INTO run_log (job, started_at, status)
           VALUES ('checkpoint', ?, 'ok')""", (f"{d} 10:00:00 JST",))
conn.commit()
# rollup so the daily table covers the whole 20 days
rollup_daily(conn)
conn.commit()
before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
          for t in ("snapshots", "cast_rankings", "scrape_log", "run_log",
                    "daily_cast_status")}
check("fixture: 20 days of each table", before["snapshots"] == 40
      and before["daily_cast_status"] == 40)

cfg = retention.retention_config({
    "snapshots_days": 15, "slot_events_days": 30, "daily_days": 365,
    "rankings_days": 365, "scrape_log_days": 90, "run_log_days": 365,
})
stats = retention.prune(conn, cfg, today)
check("prune: snapshots cut to 15 dates",
      conn.execute("SELECT COUNT(DISTINCT target_date) FROM snapshots")
      .fetchone()[0] == 15)
check("prune: newest date kept",
      conn.execute("SELECT MAX(target_date) FROM snapshots").fetchone()[0]
      == today)
check("prune: oldest in-window date kept",
      conn.execute("SELECT MIN(target_date) FROM snapshots").fetchone()[0]
      == "2026-10-22")
check("prune: 5 old dates removed from snapshots",
      stats["snapshots"] == 10)                    # 2 shops x 5 days
check("prune: rankings inside 365d untouched",
      stats["cast_rankings"] == 0)
check("prune: scrape_log inside 90d untouched", stats["scrape_log"] == 0)
check("prune: daily inside 365d untouched", stats["daily_cast_status"] == 0)
check("prune: old daily rows survive snapshot deletion",
      conn.execute("SELECT COUNT(*) FROM daily_cast_status").fetchone()[0]
      == 40)

# a second run is a no-op
stats2 = retention.prune(conn, cfg, today)
check("prune: idempotent", sum(stats2.values()) == 0)

# ------------------------------------------------- history survives prune
hist = _daily_dates(conn)
check("rollup still exposes all 20 dates", len(hist) == 20)
check("snapshots expose only the window", len(_all_dates(conn)) == 15)
lines = section_daily(conn, hist)
check("daily summary keeps 20 days after prune", len(lines) == 20)
check("summary still reports sold states",
      any("available=" in line for line in lines))

# first_seen must not call a long-time cast new after snapshots shrink
conn.execute(
    """INSERT OR IGNORE INTO snapshots (shop_id, cast_name, cast_id,
           status_raw, status_norm, target_date, fetched_time, fetched_at)
       VALUES (1, 'キャストA', 100, 'available', 'available', ?, '09:00',
               ?)""", (today, f"{today} 09:00:00 JST"))
conn.commit()
news = "\n".join(section_new_casts(conn, today))
check("first_seen survives pruning (cast is not new)",
      "初登場なし" in news or "新規cast（" not in news)

# --------------------------------------------- guard: unrolled raw days
# A day the rollup does not hold must keep its raw rows: deleting them
# would destroy the only copy. The guard reports what it kept.
conn.execute("DELETE FROM daily_cast_status WHERE target_date = ?",
             ("2026-10-21",))
conn.commit()
conn.execute(
    """INSERT OR IGNORE INTO snapshots (shop_id, cast_name, cast_id,
           status_raw, status_norm, target_date, fetched_time, fetched_at)
       VALUES (1, 'キャストA', 100, 'available', 'available', '2026-10-21',
               '10:00', '2026-10-21 10:00:00 JST')""")
conn.commit()
retention.prune(conn, cfg, today)
check("guard: day missing from the rollup keeps its snapshots",
      conn.execute("SELECT COUNT(*) FROM snapshots WHERE target_date = ?",
                   ("2026-10-21",)).fetchone()[0] == 1)
check("guard: blocked days are reported",
      retention.unrolled_snapshot_days(conn, cfg, today) == ["2026-10-21"])
check("_daily_dates: union keeps a snapshot-only day",
      "2026-10-21" in _daily_dates(conn))
rollup_daily(conn)
conn.commit()
retention.prune(conn, cfg, today)
check("guard: self-heals after the rollup catches up",
      retention.unrolled_snapshot_days(conn, cfg, today) == []
      and conn.execute("SELECT COUNT(*) FROM snapshots"
                       " WHERE target_date = ?",
                       ("2026-10-21",)).fetchone()[0] == 0)

# ------------------------------------------- report with no raw rows left
# Even if snapshots vanish entirely, the rollup must still drive the
# report instead of crashing (section_coverage used dates[-1]).
conn.execute("DELETE FROM snapshots")
conn.commit()
buf = io.StringIO()
try:
    with redirect_stdout(buf):
        tracker.print_report(conn, today)
    out = buf.getvalue()
    crashed = False
except Exception as exc:  # noqa: BLE001 - the point is to report it
    out = f"{type(exc).__name__}: {exc}"
    crashed = True
check("report survives an empty snapshots table", not crashed)
check("report still prints the rollup summary",
      out.count("casts=") == 20)
check("coverage degrades to a message, not a crash",
      "coverage を算出できません" in out)
conn.close()

# --------------------------------------------------------- VACUUM switch
db2 = SCRATCH / "vacuum.db"
conn = fresh(db2)
conn.execute("INSERT INTO shops VALUES (1, '店A', '梅田', NULL, NULL)")
for i in range(30):
    d = (date(2026, 10, 1) + timedelta(days=i)).isoformat()
    add_snapshot(conn, 1, "x" * 50, 1, d)
rollup_daily(conn)   # snapshots are only pruned for days the rollup holds
conn.commit()
conn.close()
result = retention.apply_retention(
    db2, retention.retention_config({"snapshots_days": 5}), "2026-10-30")
check("apply_retention: reports deleted rows", result["rows"] > 0)
check("apply_retention: vacuumed after deletes", result["vacuumed"] is True)
check("apply_retention: file still opens",
      sqlite3.connect(str(db2)).execute(
          "SELECT COUNT(*) FROM snapshots").fetchone()[0] == 5)

# --------------------------------------------------- legacy migration v2
legacy = SCRATCH / "legacy-v1.db"
con = sqlite3.connect(legacy)
con.executescript(
    """
    CREATE TABLE shops (shop_id INTEGER PRIMARY KEY, name TEXT NOT NULL,
        area TEXT, store_status TEXT, source_url TEXT);
    CREATE TABLE snapshots (
        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id INTEGER NOT NULL REFERENCES shops (shop_id),
        cast_name TEXT NOT NULL, cast_id INTEGER, age INTEGER,
        shift TEXT, shift_start TEXT, shift_end TEXT,
        work_minutes INTEGER, work_hours REAL, status_raw TEXT,
        status_norm TEXT NOT NULL CHECK (status_norm IN
          ('sold_out', 'closed', 'low', 'available', 'unknown', 'dummy')),
        slot_pattern TEXT, target_date TEXT NOT NULL,
        fetched_at TEXT, fetched_time TEXT,
        UNIQUE (shop_id, cast_name, target_date, fetched_time));
    CREATE INDEX idx_snapshots_castid_date
        ON snapshots (shop_id, cast_id, target_date);
    CREATE INDEX idx_snapshots_status_date
        ON snapshots (status_norm, target_date);
    CREATE TABLE daily_cast_status (
        shop_id INTEGER NOT NULL, cast_key TEXT NOT NULL, cast_id INTEGER,
        cast_name TEXT NOT NULL, target_date TEXT NOT NULL,
        status_norm TEXT NOT NULL, status_raw TEXT, work_hours REAL,
        age INTEGER, fetched_at TEXT, first_sold_out_at TEXT,
        was_low INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (shop_id, cast_key, target_date));
    INSERT INTO shops VALUES (1, '店A', '梅田', NULL, NULL);
    INSERT INTO snapshots (shop_id, cast_name, cast_id, status_norm,
        target_date, fetched_time, fetched_at)
      VALUES (1, 'a', 7, 'available', '2026-10-27', '10:00',
              '2026-10-27 10:00:00 JST'),
             (1, 'a', 7, 'available', '2026-10-27', '18:00',
              '2026-10-27 18:00:00 JST');
    INSERT INTO daily_cast_status (shop_id, cast_key, cast_id, cast_name,
        target_date, status_norm, fetched_at)
      VALUES (1, '7', 7, 'a', '2026-10-27', 'available',
              '2026-10-27 18:00:00 JST');
    PRAGMA user_version = 1;
    """
)
con.commit()
con.close()

con = common.connect_db(str(legacy))
check("migration: bumps to version 2",
      con.execute("PRAGMA user_version").fetchone()[0] == 2)
names = {r[1] for r in con.execute("PRAGMA index_list(snapshots)")}
check("migration: redundant indexes dropped",
      not {"idx_snapshots_castid_date", "idx_snapshots_status_date"} & names)
row = con.execute(
    """SELECT first_seen_at, last_seen_at FROM daily_cast_status
       WHERE cast_key = '7'""").fetchone()
check("migration: envelope backfilled from snapshots",
      row == ("2026-10-27 10:00:00 JST", "2026-10-27 18:00:00 JST"))
con.close()

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if fails else 0)
