"""Publish and inspect runner liveness."""
from __future__ import annotations
import json
from datetime import datetime
from runtrack.env import DB, HEARTBEAT_KEY, STAGING_DIR, log
from storage import storage_get, storage_put
from runtrack.db import local_max_run_id, snapshot_count
from runtrack.logs import _run_ts
from common import parse_timestamp

def _as_jst_naive(ts):
    return parse_timestamp(ts)

def write_heartbeat(job: str, detail: str,
                    finished_at: str | None = None) -> None:
    """Record the last successful run as a small R2 object.

    A stateless backup runner cannot inspect the VM, so this object is
    the handshake: `runner.py guard --max-age N` reads it and decides
    whether to take over. Snapshot count and DB size are cheap liveness
    signals that also catch "the job runs but imports nothing".

    `finished_at` is the run_log stamp of the same run. Reusing it
    verbatim keeps "did another runner publish after me?" answerable
    with a plain comparison. `max_run_id` is the monotone marker the
    cross-host comparison uses - the live DB is one AUTOINCREMENT
    sequence, so it needs no clock.
    """
    payload = {
        "job": job,
        "finished_at": finished_at or _run_ts(),
        "max_run_id": local_max_run_id(),
        "snapshots": snapshot_count(DB),
        "db_bytes": DB.stat().st_size,
        "detail": detail,
    }
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STAGING_DIR / "last_ok.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n")
    try:
        storage_put(HEARTBEAT_KEY, tmp)
    finally:
        tmp.unlink(missing_ok=True)
    log(f"heartbeat ok: {HEARTBEAT_KEY} {payload}")


def read_heartbeat() -> dict:
    """Read status/last_ok.json.

    A missing object is normal (first run, pre-migration) and returns
    {}. Any other failure propagates: a runner that cannot tell whether
    another writer is ahead must not decide on its own.
    """
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STAGING_DIR / "last_ok.json"
    try:
        if not storage_get(HEARTBEAT_KEY, tmp):
            return {}
        return json.loads(tmp.read_text())
    except NotImplementedError:
        return {}   # backend without downloads: no heartbeat semantics
    finally:
        tmp.unlink(missing_ok=True)


def heartbeat_age_min(payload: dict, now: datetime) -> float | None:
    """Minutes since the recorded success; None when unknown/unparsable."""
    finished = _as_jst_naive(payload.get("finished_at"))
    if finished is None:
        return None
    if now.tzinfo is not None:
        now = now.astimezone(now.tzinfo).replace(tzinfo=None)
    return (now - finished).total_seconds() / 60
