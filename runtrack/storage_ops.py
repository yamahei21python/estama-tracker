"""Shared object pull/publish operations."""
from __future__ import annotations
import os
from pathlib import Path
from datetime import datetime
from runtrack.env import DB, LIVE_KEY, PREV_OBJ, STAGING_DIR, log
from storage import (backend_name, gw_path, gh_live_blob, gh_restore,
                     storage_copy_object, storage_copy_verify, storage_delete,
                     storage_get, storage_put, storage_verify)
from runtrack.db import verify_db, local_last_ok, local_max_run_id, snapshot_copy
from runtrack.heartbeat import read_heartbeat
from runtrack import state
from common import parse_timestamp, now_jst

def _as_jst_naive(ts):
    return parse_timestamp(ts)

def job_pull() -> str:
    """Download the live DB (stateless runs: GitHub Actions).

    The stateless runner owns no state: everything it needs is the one
    R2 object, and the job that follows writes it back. A missing key
    is an error, not an empty start: an empty DB would be uploaded over
    the good one.

    The download lands next to the DB, is verified, and only then
    replaces it. A failed download, a corrupt gzip, or a broken SQLite
    file leaves the previous DB (and its WAL) untouched.
    """
    tmp = DB.with_name(DB.name + ".download")
    tmp.unlink(missing_ok=True)
    try:
        if not storage_get(LIVE_KEY, tmp):
            raise RuntimeError(f"{LIVE_KEY} not found in {backend_name()}")
        stats = verify_db(tmp)
        # The old WAL belongs to the old file; applying it to the new
        # one would corrupt it. Drop it only once the new DB is proven.
        for suffix in ("-wal", "-shm"):
            Path(str(DB) + suffix).unlink(missing_ok=True)
        os.replace(tmp, DB)
    finally:
        tmp.unlink(missing_ok=True)
    log(f"pull ok: {DB} {DB.stat().st_size} bytes, stats={stats}, "
        f"backend={backend_name()}")
    return f"pulled {stats['snapshots']} snapshots"
def _pull_if_remote_newer() -> bool:
    """Adopt the live DB when another runner wrote it more recently.

    The VM is the primary writer, but a GitHub Actions backup can run
    while it is down. Without this, the VM's stale copy would be
    uploaded on the next checkpoint (last writer wins) and the backup's
    observations would vanish.

    The comparison is monotone on `max_run_id` (one shared DB lineage);
    heartbeats without it (pre-migration) fall back to the timestamp.
    Equal ids with a newer stamp still mean "something else's rows" in
    the fork case, so the time check runs as well.
    """
    payload = read_heartbeat()
    local_id = local_max_run_id()
    try:
        remote_id = int(payload["max_run_id"]) if payload else None
    except (KeyError, TypeError, ValueError):
        remote_id = None
    if remote_id is not None and remote_id > local_id:
        log(f"pre-run: remote DB is ahead (run_id {remote_id} > local "
            f"{local_id}); pulling before the run")
        job_pull()
        return True
    remote = _as_jst_naive(payload.get("finished_at"))
    if remote is None:
        return False
    local_raw = local_last_ok()
    local = _as_jst_naive(local_raw)
    if local is not None and remote <= local:
        return False
    log(f"pre-run: remote DB is newer (heartbeat {payload.get('finished_at')}"
        f" > local {local_raw}); pulling before the run")
    job_pull()
    return True
def _abort_if_another_writer_published() -> None:
    """Refuse to overwrite a DB another runner published mid-run.

    Two runners can only overlap when the VM and GitHub Actions are
    both enabled (the workflow's concurrency group is GHA-internal).
    The heartbeat is written after every publish, so a remote DB holding
    rows this host does not have (run_id) - or a publish newer than this
    run's start (time; needed for the fork case, where two writers from
    the same base hand out the same run ids) - means uploading would
    drop their rows. One lost hour is recoverable; a silent overwrite
    is not. A read failure is ignored (no evidence of a conflict).
    """
    if state.RUN_STARTED_AT is None:
        return
    try:
        payload = read_heartbeat()
    except Exception as exc:
        log(f"upload: heartbeat check skipped ({type(exc).__name__}: {exc})")
        return
    try:
        remote_id = int(payload.get("max_run_id")) if payload else None
    except (TypeError, ValueError):
        remote_id = None
    if remote_id is not None:
        local_id = local_max_run_id()
        if remote_id > local_id:
            raise RuntimeError(
                "another runner published a newer DB "
                f"(run_id {remote_id} > local {local_id}); "
                "refusing to overwrite its rows")
    ts = _as_jst_naive(payload.get("finished_at"))
    if ts is not None and ts > state.RUN_STARTED_AT:
        raise RuntimeError(
            "another runner published after this run started "
            f"({payload.get('finished_at')}); refusing to overwrite its DB")


def upload_db() -> dict:
    """Verify -> snapshot -> one write -> verify -> roll back on failure.

    The DB goes straight to LIVE_KEY as a single object write. The
    written object is verified against the locally verified snapshot;
    only a mismatch re-uploads it once. If that also fails the previous
    live object is restored, so the shared copy never stays corrupt:
    GitHub via the pre-captured blob SHA, gateway via the server-side
    copy of `PREV_OBJ` taken just before the write. When the gateway
    has no previous object (first publish) or the copy failed, there is
    nothing to roll back to and the failure is reported as-is.
    """
    _abort_if_another_writer_published()
    stats = verify_db(DB)
    staging = STAGING_DIR / f"estama-{now_jst():%Y%m%d-%H%M%S}.db"
    size = 0
    copied_prev = False
    if backend_name() == "gateway":
        try:
            # Snapshot the current live object *before* overwriting it.
            storage_copy_object(gw_path(LIVE_KEY), PREV_OBJ)
            copied_prev = True
            log(f"upload: live object snapshot -> {PREV_OBJ}")
        except Exception as exc:
            # A missing live object (first publish) is the normal case;
            # anything else just means no rollback for this run.
            log(f"upload: prev copy skipped ({type(exc).__name__}: {exc})")
    try:
        snapshot_copy(DB, staging)
        verify_db(staging)  # the copy itself must be intact before it can go live
        size = staging.stat().st_size
        prev_blob = (gh_live_blob(LIVE_KEY)
                     if backend_name() == "github" else None)
        storage_put(LIVE_KEY, staging)
        if not storage_verify(LIVE_KEY, staging):
            storage_put(LIVE_KEY, staging)  # retry once from the good copy
        if not storage_verify(LIVE_KEY, staging):
            if backend_name() == "github":
                if prev_blob:
                    gh_restore(LIVE_KEY, prev_blob)
                    log(f"upload: verified live write failed, restored {LIVE_KEY}")
                else:
                    storage_delete(LIVE_KEY)
                    log(f"upload: verified live write failed, deleted {LIVE_KEY}")
            elif backend_name() == "gateway" and copied_prev:
                try:
                    storage_copy_object(PREV_OBJ, gw_path(LIVE_KEY))
                    if storage_copy_verify(PREV_OBJ, gw_path(LIVE_KEY)):
                        log(f"upload: live object restored from {PREV_OBJ}")
                    else:
                        log("upload: rollback copied, but size/ETag do not match")
                except Exception as exc:
                    log(f"upload: rollback FAILED ({type(exc).__name__}: {exc})")
            raise IOError("live object verification failed after restore")
    finally:
        staging.unlink(missing_ok=True)
    log(f"upload ok: {LIVE_KEY} {size} bytes, stats={stats}, backend={backend_name()}")
    return stats
