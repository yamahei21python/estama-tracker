"""DB-side verification helpers: integrity, snapshots, counts, recency."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from common import now_jst  # noqa: F401  (re-export for callers/tests)

from runtrack.env import DB, log


def verify_db(path: Path) -> dict:
    """Raise ValueError unless the DB is intact and non-trivial."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"integrity_check failed: {integrity}")
        stats = {
            "shops": con.execute("SELECT COUNT(*) FROM shops").fetchone()[0],
            "snapshots": con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
            "rankings": con.execute(
                "SELECT COUNT(*) FROM cast_rankings").fetchone()[0],
        }
        if stats["shops"] < 1 or stats["snapshots"] < 1:
            raise ValueError(f"suspicious row counts: {stats}")
        return stats
    finally:
        con.close()


def snapshot_copy(src: Path, dst: Path) -> None:
    """Copy via the SQLite backup API (safe against a live writer)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()


def snapshot_count(path: Path) -> int:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    finally:
        con.close()


def local_last_ok() -> str | None:
    """finished_at of the newest successful local run (None when unknown)."""
    if not DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            return con.execute(
                "SELECT MAX(finished_at) FROM run_log WHERE status = 'ok'"
            ).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def local_max_run_id() -> int:
    """Newest run_log id in the local DB (0 when unknown).

    The live DB is the shared artifact every writer pulls first, so
    run ids come from one AUTOINCREMENT sequence and are comparable
    across hosts. A remote heartbeat with a higher id holds rows this
    host does not have - no clock involved.
    """
    if not DB.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT COALESCE(MAX(run_id), 0) FROM run_log").fetchone()
            return int(row[0] or 0)
        finally:
            con.close()
    except (sqlite3.Error, TypeError, ValueError):
        return 0
