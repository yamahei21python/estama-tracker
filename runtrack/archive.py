"""Daily archives and retention."""
from __future__ import annotations
import json
import re
import sqlite3
from datetime import datetime, timedelta
from runtrack.env import DB, STATE_FILE, STAGING_DIR, log
from storage import storage_config, storage_exists, storage_list, storage_put, storage_delete, storage_verify
from runtrack.db import snapshot_copy, verify_db
from policy import archive_retention_config, db_retention_config
from retention import apply_retention, retention_config
from common import business_date, now_jst
from runtrack.storage_ops import _pull_if_remote_newer

def expected_business_date(now: datetime) -> str:
    return business_date(now)


def maybe_archive(now: datetime) -> str | None:
    """Archive the just-finished business day once.

    Keyed on the CLOSED business date (expected - 1), not on
    MAX(target_date): when the site flips before the last checkpoint of
    the day, that run already imports the new date and a MAX-based
    check would never archive the finished day at all."""
    closed = (datetime.fromisoformat(expected_business_date(now))
              - timedelta(days=1)).date().isoformat()
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    if state.get("last_archived") == closed:
        return None
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            has_day = con.execute(
                "SELECT EXISTS(SELECT 1 FROM snapshots"
                " WHERE target_date = ?)", (closed,)).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not has_day:
        return None
    key = f"archive/estama-{closed}.db"
    if not storage_exists(key):
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        snap = STAGING_DIR / f"archive-{closed}.db"
        try:
            snapshot_copy(DB, snap)
            verify_db(snap)
            storage_put(key, snap)
            if not storage_verify(key, snap):
                # leave no unverifiable object behind, or the next run's
                # storage_exists() would skip the repair forever
                storage_delete(key)
                raise IOError(f"archive upload verification failed for {key}")
        finally:
            snap.unlink(missing_ok=True)
    state["last_archived"] = closed
    STATE_FILE.write_text(json.dumps(state))
    log(f"archive ok: {key} (business date {closed} closed)")
    return key


# ------------------------------------------------------ archive retention ---
# Optional bounded retention for archive/estama-<date>.db. Off unless
# storage.json carries {"archive_retention": {"days": 30, "weekly": true}}.
# Everything newer than `days` is kept; older archives are kept only when
# their date falls on a Monday (weekly=true), which bounds growth without
# losing a coarse history.

ARCHIVE_KEY_RE = re.compile(r"^archive/estama-(\d{4}-\d{2}-\d{2})\.db(?:\.gz)?$")


def stale_archive_keys(keys: list[str], today, days: int,
                       weekly: bool) -> list[str]:
    cutoff = today - timedelta(days=days)
    stale = []
    for key in keys:
        m = ARCHIVE_KEY_RE.match(key)
        if not m:
            continue  # never touch anything we cannot date-parse
        day = datetime.fromisoformat(m.group(1)).date()
        if day >= cutoff:
            continue
        if weekly and day.weekday() == 0:
            continue
        stale.append(key)
    return sorted(stale)


def prune_archives(now: datetime) -> list[str]:
    cfg = archive_retention_config(storage_config().get("archive_retention"))
    days = cfg.get("days")
    if not days:
        return []  # unset: no deletion
    try:
        keys = storage_list("archive/")
    except NotImplementedError as exc:
        # The gateway has no LIST endpoint: pruning cannot enumerate
        # keys there. Never swallow this into a silent no-op.
        log(f"archive retention: cannot list keys ({exc}); "
            f"no archive was deleted")
        return []
    stale = stale_archive_keys(
        keys, now.date(), int(days),
        bool(cfg.get("weekly", True)))
    for key in stale:
        storage_delete(key)
        log(f"archive retention: deleted {key}")
    if stale:
        log(f"archive retention: {len(stale)} old archive(s) removed")
    return stale


def _retention_best_effort() -> None:
    try:
        prune_archives(now_jst())
    except Exception as exc:
        log(f"archive retention failed (job continues): {type(exc).__name__}: {exc}")
def _last_archived() -> str | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text()).get("last_archived")
    except (ValueError, OSError):
        return None


def _db_retention_best_effort() -> None:
    """Delete rows past their retention window, then VACUUM.

    Runs after the archive step and after the import, so a removed day
    already exists as `archive/estama-<date>.db`. Refuses to delete when
    archiving has stalled (no recent last_archived): the archive is the
    raw record, and silently dropping unarchived days would be
    unrecoverable. Best effort: a retention failure must not block the
    upload of a good scrape.
    """
    cfg = None
    today = business_date(now_jst())
    try:
        cfg = retention_config(
            db_retention_config(storage_config().get("db_retention")))
        if not cfg:
            return  # key absent: retention disabled
        last = _last_archived()
        if not last:
            log("db retention: skipped (no archive recorded yet)")
            return
        lag = (datetime.fromisoformat(today).date()
               - datetime.fromisoformat(last).date()).days
        if lag > 2:
            log(f"db retention: skipped (last archive {last}, {lag}d behind)")
            return
        result = apply_retention(DB, cfg, today)
    except Exception as exc:
        # Config/state problems must never fail a checkpoint: the rows
        # simply stay until the next run.
        log(f"db retention failed (job continues): {type(exc).__name__}: {exc}")
        return
    removed = {k: v for k, v in result["deleted"].items() if v}
    log(f"db retention: rows={result['rows']} "
        f"vacuum={'yes' if result['vacuumed'] else 'no'} {removed}")
    if result["unrolled"]:
        log(f"db retention: kept unrolled snapshot days {result['unrolled']}"
            " (rollup missing; will retry)")


def _pre_run_pull() -> None:
    """Adopt a newer remote DB before this run writes anything.

    If the heartbeat cannot be read, stop: silently overwriting a
    backup run's rows (last writer wins) is worse than skipping one
    hour, and the scrape could not have been published anyway.
    """
    try:
        _pull_if_remote_newer()
    except NotImplementedError:
        return  # backend without downloads
    except Exception as exc:
        raise RuntimeError(
            f"cannot check the remote heartbeat: {type(exc).__name__}: {exc}"
        ) from exc
