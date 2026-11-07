"""Run-log bookkeeping (status rows in the shared DB)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from common import stamp  # noqa: F401

from runtrack.env import CODE_DIR, DB, log


def _run_ts() -> str:
    return stamp()


def _run_db() -> sqlite3.Connection:
    """DB connection for run_log writes, with the schema applied.

    Goes through app/common.connect_db so a pre-upgrade estama.db gets
    the run_log / daily_cast_status tables on first touch.
    """
    # code lives in the repo's app/ dir; APP is the data dir (redirected
    # by ESTAMA_VM_BASE in tests/staging)
    sys.path.insert(0, str(CODE_DIR / "app"))
    from common import connect_db
    return connect_db(DB)


def run_log_open(job: str) -> int | None:
    """Insert a running row; returns run_id (None on failure)."""
    try:
        con = _run_db()
        cur = con.execute(
            "INSERT INTO run_log (job, started_at, status)"
            " VALUES (?, ?, 'running')",
            (job, _run_ts()))
        con.commit()
        con.close()
        return cur.lastrowid
    except Exception as exc:
        log(f"run_log open failed: {exc}")
        return None


def run_log_close(run_id: int | None, status: str, detail: str | None = None) -> str | None:
    """Finish a run row; returns the finished_at stamp it wrote.

    The heartbeat reuses that exact string, so a runner comparing its
    own last success against the remote heartbeat sees equality instead
    of a phantom "remote is newer" a second later.
    """
    ts = _run_ts()
    if run_id is None:
        return None
    try:
        con = _run_db()
        con.execute(
            "UPDATE run_log SET finished_at = ?, status = ?, detail = ?"
            " WHERE run_id = ?",
            (ts, status, detail, run_id))
        con.commit()
        con.close()
    except Exception as exc:
        log(f"run_log close failed: {exc}")
        return None
    return ts


def run_log_mark(job: str, status: str, detail: str | None = None) -> None:
    """One-shot completed row (e.g. a lock SKIP that never ran)."""
    run_id = run_log_open(job)
    run_log_close(run_id, status, detail)
