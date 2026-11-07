#!/usr/bin/env python3
"""estama-tracker runner compatibility shim.

実装は runtrack/ パッケージに移行済み。ここは旧テストの monkeypatch
境界を維持する薄い wrapper + JOBS + main() のみ。
"""
from __future__ import annotations

import fcntl
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(CODE_DIR / "app"))

# 環境・定数（テストが ESTAMA_VM_BASE でリダイレクトする）
from runtrack.env import (  # noqa: E402
    APP, BASE, DASHBOARD, DB, HEARTBEAT_KEY, LIVE_KEY, LOCK_FILE, LOG_DIR,
    PREV_OBJ, STAGING_DIR, STATE_FILE, VENV_PY, log,
)
from runtrack.state import CURRENT_RUN_ID, RUN_STARTED_AT  # noqa: E402,F401

from common import (  # noqa: E402
    business_date, now_jst, parse_timestamp, stamp,  # noqa: F401
)
from retention import apply_retention, retention_config  # noqa: E402,F401
from policy import archive_retention_config, db_retention_config  # noqa: E402,F401
from storage import (  # noqa: E402
    backend_name, gh_live_blob, gh_restore, gw_path,  # noqa: F401
    storage_config, storage_copy_object, storage_copy_verify,  # noqa: F401
    storage_delete, storage_exists, storage_get, storage_list,  # noqa: F401
    storage_put, storage_verify,  # noqa: F401
)

# runtrack 実装群
from runtrack import archive, ban, db as rdb, guard, heartbeat, jobs, logs, scraper, storage_ops  # noqa: E402


# ---------------------------------------------------------------- DB helpers --
def verify_db(path):
    return rdb.verify_db(path)


def snapshot_copy(src, dst):
    return rdb.snapshot_copy(src, dst)


def snapshot_count(path):
    return rdb.snapshot_count(path)


def local_last_ok():
    return rdb.local_last_ok()


def local_max_run_id():
    return rdb.local_max_run_id()


# --------------------------------------------------------------- storage ops --
def _as_jst_naive(ts):
    return parse_timestamp(ts)


def _sync_run_started_at():
    # Shim trap: the module global stays None until main() sets it, so an
    # unconditional copy would erase a value set directly on
    # runtrack.state (the runtrack-native way, e.g. by tests).
    import runtrack.state as state
    started = globals().get('RUN_STARTED_AT')
    if started is not None:
        state.RUN_STARTED_AT = started


def _pull_if_remote_newer():
    _sync_run_started_at()
    return storage_ops._pull_if_remote_newer()


def _abort_if_another_writer_published():
    _sync_run_started_at()
    return storage_ops._abort_if_another_writer_published()


def upload_db():
    return storage_ops.upload_db()


# ---------------------------------------------------------------- run_log -----
def _run_ts():
    return logs._run_ts()


def _run_db():
    return logs._run_db()


def run_log_open(job):
    return logs.run_log_open(job)


def run_log_close(run_id, status, detail=None):
    return logs.run_log_close(run_id, status, detail)


def run_log_mark(job, status, detail=None):
    return logs.run_log_mark(job, status, detail)


# ------------------------------------------------------------------ scraper ---
def run_scraper(args, timeout=1800):
    return scraper.run_scraper(args, timeout)


# ---------------------------------------------------------------- archive -----
def expected_business_date(now):
    return archive.expected_business_date(now)


def maybe_archive(now):
    return archive.maybe_archive(now)


def stale_archive_keys(keys, today, days, weekly=True):
    return archive.stale_archive_keys(keys, today, days, weekly)


def prune_archives(now):
    return archive.prune_archives(now)


def _retention_best_effort():
    return archive._retention_best_effort()


def _last_archived():
    return archive._last_archived()


def _db_retention_best_effort():
    return archive._db_retention_best_effort()


def _pre_run_pull():
    return archive._pre_run_pull()


# -------------------------------------------------------------------- ban -----
def _load_json(path, default):
    return ban._load_json(path, default)


def shop_stats(con, shop_id):
    return ban.shop_stats(con, shop_id)


def prune_candidates(db_path, today_bd):
    return ban.prune_candidates(db_path, today_bd)


def apply_bans(candidates, today_bd):
    return ban.apply_bans(candidates, today_bd)


def enforce_ban():
    return ban.enforce_ban()


# ------------------------------------------------------------------ jobs ------
def job_pull():
    return storage_ops.job_pull()


def job_prune():
    return jobs.job_prune()


def job_checkpoint():
    return jobs.job_checkpoint()


def job_ranking():
    return jobs.job_ranking()


# --------------------------------------------------------------- heartbeat ----
def write_heartbeat(job, detail, finished_at=None):
    return heartbeat.write_heartbeat(job, detail, finished_at)


def read_heartbeat():
    return heartbeat.read_heartbeat()


def heartbeat_age_min(payload, now):
    return heartbeat.heartbeat_age_min(payload, now)


# -------------------------------------------------------------------- guard ---
def job_guard(max_age_min):
    return guard.job_guard(max_age_min)


# ---------------------------------------------------------------- publish -----
def publish():
    return jobs.publish()


# ================================================================ dispatch ====
class Job(NamedTuple):
    """One operational job.

    publishes: whether the job's output is pushed to the shared store.
    Checkpoint and ranking publish; prune only edits local state and
    never writes to the shared DB (its edits travel with the next
    publish).
    """
    run: Callable[[], str]
    publishes: bool


JOBS: dict[str, Job] = {
    "checkpoint": Job(job_checkpoint, True),
    "ranking": Job(job_ranking, True),
    "prune": Job(job_prune, False),
}


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] not in (*JOBS, "pull", "guard"):
        print(__doc__)
        return 2
    job = argv[0]
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"SKIP {job}: another run holds the lock")
        run_log_mark(job, "skip", "another run holds the lock")
        return 0
    if job == "guard":
        max_age = 90.0
        if "--max-age" in argv:
            try:
                max_age = float(argv[argv.index("--max-age") + 1])
            except (IndexError, ValueError):
                log("guard: --max-age needs a number")
                return 2
        try:
            return job_guard(max_age)
        except Exception as exc:
            log(f"guard failed: {type(exc).__name__}: {exc}")
            return 3
    if job == "pull":
        try:
            detail = job_pull()
        except Exception as exc:
            log(f"FAIL pull: {type(exc).__name__}: {exc}")
            return 1
        log(f"DONE pull: {detail}")
        return 0
    log(f"START {job}")
    # ノート: RUN_STARTED_AT は naive JST。
    # 外部 heartbeat の parse_timestamp も naive JST を返すため、比較可能。
    RUN_STARTED_AT = now_jst().replace(tzinfo=None)
    run_id = run_log_open(job)
    import runtrack.state as state
    state.RUN_STARTED_AT = RUN_STARTED_AT
    state.CURRENT_RUN_ID = run_id
    try:
        detail = JOBS[job].run()
    except Exception as exc:
        log(f"FAIL {job}: {type(exc).__name__}: {exc}")
        run_log_close(run_id, "fail", f"{type(exc).__name__}: {exc}")
        return 1
    finished_at = run_log_close(run_id, "ok", detail)
    if JOBS[job].publishes:
        try:
            publish()
        except Exception as exc:
            log(f"publish failed after ok: {type(exc).__name__}: {exc}")
            run_log_close(run_id, "fail",
                          f"job ok, publish failed: {type(exc).__name__}: {exc}")
            return 1
        _retention_best_effort()
        try:
            write_heartbeat(job, detail, finished_at)
        except Exception as exc:
            log(f"heartbeat failed (job ok): {type(exc).__name__}: {exc}")
    log(f"DONE {job}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
