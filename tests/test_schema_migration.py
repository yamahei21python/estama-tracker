"""Tests for the PRAGMA user_version schema migrations in common.py:
legacy index cleanup, the index set created by the snapshots rebuild,
idempotency, and the query plan of time-series reads.

Run: python3.11 tests/test_schema_migration.py
"""
import atexit
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="estama-schema-test-"))
atexit.register(shutil.rmtree, SCRATCH, True)
sys.path.insert(0, str(ROOT / "app"))
import common  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


def index_names(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})")}


def order_plan(conn, sql):
    return [r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)]


# ------------------------------------------------------------- fresh DB
fresh = SCRATCH / "fresh.db"
con = common.connect_db(str(fresh))
check("fresh DB user_version = 1",
      con.execute("PRAGMA user_version").fetchone()[0] == 1)
names = index_names(con, "snapshots")
check("fresh DB has (target_date, fetched_at) index",
      "idx_snapshots_date_at" in names)
check("fresh DB has cast_id index", "idx_snapshots_castid_date" in names)
check("fresh DB has no fetched_time / work_hours index",
      not {"idx_snapshots_time", "idx_snapshots_work_hours"} & names)
con.close()

# ------------------------------------------- legacy DB: old indexes only
legacy = SCRATCH / "legacy.db"
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
        work_minutes INTEGER, work_hours REAL,
        status_raw TEXT,
        status_norm TEXT NOT NULL CHECK (
            status_norm IN ('sold_out', 'closed', 'low', 'available', 'unknown', 'dummy')),
        slot_pattern TEXT, target_date TEXT NOT NULL,
        fetched_at TEXT, fetched_time TEXT,
        UNIQUE (shop_id, cast_name, target_date, fetched_time));
    CREATE INDEX idx_snapshots_time ON snapshots (target_date, fetched_time);
    CREATE INDEX idx_snapshots_work_hours ON snapshots (work_hours);
    INSERT INTO shops VALUES (1, '店A', '梅田', NULL, NULL);
    INSERT INTO snapshots (shop_id, cast_name, status_norm, target_date,
        fetched_at, fetched_time)
      VALUES (1, 'a', 'available', '2026-10-27', '2026-10-27 10:00', '10:00');
    """
)
con.commit()
con.close()

con = common.connect_db(str(legacy))
check("legacy DB migrated to user_version 1",
      con.execute("PRAGMA user_version").fetchone()[0] == 1)
names = index_names(con, "snapshots")
check("legacy DB: old indexes dropped",
      not {"idx_snapshots_time", "idx_snapshots_work_hours"} & names)
check("legacy DB: new indexes added",
      {"idx_snapshots_date_at", "idx_snapshots_castid_date"} <= names)
check("legacy DB: rows kept",
      con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1)
plan = order_plan(
    con,
    "SELECT * FROM snapshots WHERE target_date = '2026-10-27'"
    " ORDER BY fetched_at")
check("ORDER BY fetched_at uses idx_snapshots_date_at",
      any("idx_snapshots_date_at" in step for step in plan))
check("ORDER BY fetched_at builds no temp b-tree",
      not any("TEMP B-TREE" in step for step in plan))
con.close()

# idempotency: a second open is a no-op and keeps the version
con = common.connect_db(str(legacy))
check("re-open keeps user_version 1",
      con.execute("PRAGMA user_version").fetchone()[0] == 1)
check("re-open keeps index set",
      {"idx_snapshots_date_at", "idx_snapshots_castid_date"}
      <= index_names(con, "snapshots"))
con.close()

# ------------------- pre-'dummy' CHECK DB: rebuild must create new indexes
old_check = SCRATCH / "old_check.db"
con = sqlite3.connect(old_check)
con.executescript(
    """
    CREATE TABLE shops (shop_id INTEGER PRIMARY KEY, name TEXT NOT NULL,
        area TEXT, store_status TEXT, source_url TEXT);
    CREATE TABLE snapshots (
        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_id INTEGER NOT NULL REFERENCES shops (shop_id),
        cast_name TEXT NOT NULL, cast_id INTEGER, age INTEGER,
        shift TEXT, shift_start TEXT, shift_end TEXT,
        work_minutes INTEGER, work_hours REAL,
        status_raw TEXT,
        status_norm TEXT NOT NULL CHECK (
            status_norm IN ('sold_out', 'closed', 'low', 'available', 'unknown')),
        slot_pattern TEXT, target_date TEXT NOT NULL,
        fetched_at TEXT, fetched_time TEXT,
        UNIQUE (shop_id, cast_name, target_date, fetched_time));
    INSERT INTO shops VALUES (1, '店A', '梅田', NULL, NULL);
    INSERT INTO snapshots (shop_id, cast_name, status_norm, target_date)
      VALUES (1, 'a', 'available', '2026-10-27');
    """
)
con.commit()
con.close()

con = common.connect_db(str(old_check))
names = index_names(con, "snapshots")
check("CHECK rebuild: 'dummy' now accepted",
      "dummy" in con.execute(
          "SELECT sql FROM sqlite_master WHERE name='snapshots'").fetchone()[0])
check("CHECK rebuild: new index set present",
      {"idx_snapshots_date_at", "idx_snapshots_castid_date"} <= names)
check("CHECK rebuild: old indexes absent",
      not {"idx_snapshots_time", "idx_snapshots_work_hours"} & names)
check("CHECK rebuild: rows kept",
      con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1)
con.close()

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
