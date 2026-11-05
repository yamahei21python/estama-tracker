"""Bounded retention for the live DB (size control for the R2 upload).

The Worker accepts request bodies up to 100MB, so the gzipped DB has to
stay well below that forever. Two things bound it:

* raw snapshots are only needed for intraday work (feature checks,
  slot-level analysis), so they are dropped after a short window;
* the daily rollup (`daily_cast_status`) carries the long-horizon
  history (status, first sold-out time, work hours, observation
  envelope) at one row per cast-day -- ~96% smaller than the snapshots
  it was built from.

Every table is deleted by its own date column, never by row id, so a run
that is interrupted halfway is still consistent: the next run picks up
where this one stopped.

Safety rules (the caller is responsible for the first two):
* only runs after the daily archive succeeded (the archive is the raw
  record and has to outlive the live rows);
* never deletes the newest day, because the cutoff is derived from the
  business date and is always in the past;
* raw snapshots are only deleted for days the rollup already holds, so
  a rollup that never ran can never destroy the only copy of a day
  (the blocked days are returned so the caller can log them);
* a disabled table (days = 0 / null / missing) is left untouched, and a
  malformed value disables the table: an empty or broken config prunes
  nothing.

Config (checked at run time, never cached across hosts):

    config/policy.json   repo-wide defaults (single source of truth)
    storage.json         "db_retention" overrides one or more keys per host

    {
      "snapshots_days": 15,
      "slot_events_days": 30,
      "daily_days": 365,
      "rankings_days": 365,
      "scrape_log_days": 90,
      "run_log_days": 365
    };
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

# Chosen from the 2026-11-05 measurements: snapshots cost ~1.3MB gz/day,
# slot_events ~0.42MB/day, and the auxiliary tables ~0.11MB/day. This
# set plateaus around 60MB gz (~60% of the hard 100MB limit).
DEFAULTS = {
    "snapshots_days": 15,
    "slot_events_days": 30,
    "daily_days": 365,
    "rankings_days": 365,
    "scrape_log_days": 90,
    "run_log_days": 365,
}

# (table, date expression, config key, extra predicate). Expressions
# stay SQL literals: they are fixed strings, never user input.
#
# snapshots carry the guard: a raw day is only deleted once the rollup
# holds it, so a rollup that never ran (fresh clone, interrupted job,
# manual CLI run) can never destroy the only copy of a day. The next
# successful rollup lets the guard through, so this self-heals.
DELETES = (
    ("snapshots", "target_date", "snapshots_days",
     " AND target_date IN (SELECT target_date FROM daily_cast_status)"),
    ("slot_events", "target_date", "slot_events_days", ""),
    ("daily_cast_status", "target_date", "daily_days", ""),
    ("cast_rankings", "fetched_date", "rankings_days", ""),
    ("scrape_log", "substr(fetched_at, 1, 10)", "scrape_log_days", ""),
    ("run_log", "substr(started_at, 1, 10)", "run_log_days", ""),
)

# VACUUM rewrites the whole file; cheap at this size (1.1s for 129MB) but
# pointless when almost nothing was freed.
VACUUM_MIN_FREELIST_PAGES = 200


def retention_config(raw) -> dict | None:
    """Merge storage.json's db_retention with DEFAULTS.

    Returns None when the key is absent: no config means no deletion.
    A value of 0/None/"" disables that one table. A malformed value
    (typo like "15d") disables that table too, rather than falling back
    to the default: a config mistake must never start deleting rows.
    """
    if not isinstance(raw, dict):
        return None
    cfg = {}
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if value is None:
            cfg[key] = None
            continue
        try:
            days = int(value)
        except (TypeError, ValueError):
            cfg[key] = None
            continue
        cfg[key] = days if days > 0 else None
    return cfg


def cutoff(today: str, days: int) -> str:
    """Oldest date to KEEP: `days` calendar dates ending at `today`."""
    return (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()


def prune(conn: sqlite3.Connection, cfg: dict, today: str) -> dict[str, int]:
    """Delete rows older than each table's window. Returns row counts."""
    stats: dict[str, int] = {}
    for table, expr, key, guard in DELETES:
        days = cfg.get(key)
        if not days:
            continue
        oldest = cutoff(today, int(days))
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {expr} < ?{guard}", (oldest,))
        stats[table] = cur.rowcount
    conn.commit()
    return stats


def unrolled_snapshot_days(conn: sqlite3.Connection, cfg: dict,
                           today: str) -> list[str]:
    """Dates past the snapshot window that the rollup does not hold yet.

    Non-empty means the guard blocked a deletion (rollup missing): the
    raw rows are kept and the caller should surface it, not ignore it.
    """
    days = cfg.get("snapshots_days")
    if not days:
        return []
    oldest = cutoff(today, int(days))
    try:
        rows = conn.execute(
            """SELECT DISTINCT target_date FROM snapshots
               WHERE target_date < ?
                 AND target_date NOT IN
                     (SELECT target_date FROM daily_cast_status)
               ORDER BY 1""", (oldest,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows]


def vacuum_if_needed(conn: sqlite3.Connection, deleted: int) -> bool:
    """VACUUM when rows were removed or the file is visibly fragmented."""
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    if deleted <= 0 and freelist < VACUUM_MIN_FREELIST_PAGES:
        return False
    conn.execute("VACUUM")
    return True


def apply_retention(db_path, cfg: dict, today: str) -> dict:
    """Prune + VACUUM one DB. Returns counts and whether it vacuumed."""
    conn = sqlite3.connect(str(db_path))
    try:
        stats = prune(conn, cfg, today)
        blocked = unrolled_snapshot_days(conn, cfg, today)
        deleted = sum(stats.values())
        vacuumed = vacuum_if_needed(conn, deleted)
    finally:
        conn.close()
    return {"deleted": stats, "rows": deleted, "vacuumed": vacuumed,
            "unrolled": blocked}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Apply DB retention once.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--today", default=date.today().isoformat(),
                    help="business date (YYYY-MM-DD); default today")
    ap.add_argument("--config", help="storage.json to read db_retention from")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the cutoffs without deleting")
    args = ap.parse_args(argv)

    cfg = None
    if args.config:
        raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
        cfg = retention_config(raw.get("db_retention"))
    if cfg is None:
        cfg = dict(DEFAULTS)

    if args.dry_run:
        for table, expr, key, guard in DELETES:
            days = cfg.get(key)
            print(f"{table:20} keep={days} cutoff="
                  f"{cutoff(args.today, int(days)) if days else 'disabled'}")
        return 0

    result = apply_retention(args.db, cfg, args.today)
    for table, n in result["deleted"].items():
        print(f"{table:20} deleted={n}")
    if result["unrolled"]:
        print(f"kept snapshots (not rolled up yet): {result['unrolled']}")
    print(f"rows={result['rows']} vacuum={'yes' if result['vacuumed'] else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
