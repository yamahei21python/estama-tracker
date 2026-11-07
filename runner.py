#!/usr/bin/env python3
"""estama-tracker runner (Muse VM).

Wraps the scrapers with the operational safeguards agreed 2026-10-26:

1. Single-instance lock (fcntl.flock) shared by checkpoint and ranking jobs,
   so a slow run can never overlap the next one and corrupt the SQLite DB.
2. Upload safety: the DB is verified (PRAGMA integrity_check + row counts)
   and snapshotted through the SQLite backup API *before* upload. The live
   object is then written in one commit and verified against that snapshot
   (GitHub: blob SHA); on a verification failure the write is retried once
   and, if it still fails, GitHub restores the previous blob (or deletes the
   key when there was none) so the branch never keeps a corrupt DB.
3. Business-date daily archive: keyed on the CLOSED business date (expected
   business date - 1), not on MAX(target_date). When the site flips before
   the last checkpoint of the day, a MAX-based check would never archive the
   finished day. `archive/estama-<closed-date>.db` is written once.
4. Bounded DB retention: after the import, rows past their window
   (storage.json `db_retention`) are deleted and the file is VACUUMed, so
   the gzipped upload stays far below the Worker's 100MB body limit.
   Skipped when archiving has stalled: the archive is the raw record.

Storage backends (write live key + verify; GitHub restores on mismatch):
- GitHub: when BASE/storage.json says {"backend": "github", ...}, objects are
  stored as files on a dedicated `data` branch of the code repo (DB files
  gzip-compressed). Interim backend until the R2 migration; auth rides the
  custom.github-cli connector surrogate, so no token sits on this VM.
- Cloudflare R2 via boto3 when ESTAMA_R2_* env vars are set.
- Otherwise a local mirror directory (BASE/r2_local/).

Usage:
    runner.py checkpoint    # scrape all shops (today only) + verify + upload
    runner.py ranking       # weekly rankings + watchlist sync + upload
    runner.py pull          # download the live DB (stateless: GitHub Actions)
    runner.py guard --max-age 90
                            # exit 0 when the heartbeat is fresh (VM alive),
                            # 3 when a backup runner should take over
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
from typing import Callable, NamedTuple
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(os.environ.get("ESTAMA_VM_BASE", Path(__file__).resolve().parent))
APP = BASE / "app"
DB = APP / "estama.db"
VENV_PY = BASE / "venv" / "bin" / "python"
if not VENV_PY.exists():
    # GitHub Actions / bare checkouts have no venv; use the interpreter
    # that is running this file.
    VENV_PY = Path(sys.executable)
LOCK_FILE = BASE / "runner.lock"
STATE_FILE = BASE / "archive_state.json"
LOG_DIR = BASE / "logs"
STAGING_DIR = BASE / "staging"

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(CODE_DIR / "app"))
from common import business_date, now_jst  # noqa: E402
from policy import archive_retention_config, db_retention_config  # noqa: E402
from retention import apply_retention, retention_config  # noqa: E402
from storage import (  # noqa: E402
    backend_name, gw_path, gh_live_blob, gh_restore, storage_config,
    storage_copy_object, storage_copy_verify, storage_delete,
    storage_exists, storage_get, storage_list, storage_put,
    storage_verify,
)

LIVE_KEY = "estama.db"
# Object key holding the live DB as it was *before* the current publish.
# Written with a server-side R2 copy, so it can be rolled back without
# downloading anything. Overwritten on every publish.
PREV_OBJ = "estama.db.prev.gz"
# Small object the schedule/backup logic reads. Written after every
# successful publish so a stateless runner (GitHub Actions) can tell
# whether the VM is still doing its job.
HEARTBEAT_KEY = "status/last_ok.json"


def log(msg: str) -> None:
    ts = now_jst().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / f"runner-{now_jst():%Y-%m-%d}.log", "a") as fh:
        fh.write(line + "\n")


# ------------------------------------------------------------------- DB -----

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
            "rankings": con.execute("SELECT COUNT(*) FROM cast_rankings").fetchone()[0],
        }
    finally:
        con.close()
    if stats["shops"] == 0 or stats["snapshots"] == 0:
        raise ValueError(f"DB looks empty: {stats}")
    return stats


def snapshot_copy(src: Path, dst: Path) -> None:
    """Consistent copy via the SQLite backup API (safe while src is live)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def snapshot_count(path: Path) -> int:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    finally:
        con.close()


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


def _as_jst_naive(ts: str | None) -> datetime | None:
    """Parse a run_log / heartbeat timestamp to naive JST for comparison.

    run_log writes `"%Y-%m-%d %H:%M:%S JST"` and the heartbeat writes
    ISO with an offset; both have to be comparable.
    """
    if not ts:
        return None
    ts = ts.strip()
    if ts.endswith(" JST"):
        ts = ts[:-4]
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(now_jst().tzinfo).replace(tzinfo=None)
    return dt


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


# ---------------------------------------------------------------- upload ----

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
    if RUN_STARTED_AT is None:
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
    if ts is not None and ts > RUN_STARTED_AT:
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


# --------------------------------------------------------------- archive ----

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


# ---------------------------------------------------------------- run_log ---
# One run_log row per job so scrape failures and lock skips are
# queryable in the DB itself (not only the runner log files). Writes
# are best-effort: a run_log failure must never break a job.

def _run_ts() -> str:
    return now_jst().strftime("%Y-%m-%d %H:%M:%S") + " JST"


def _run_db() -> sqlite3.Connection:
    """DB connection for run_log writes, with the schema applied.

    Goes through app/common.connect_db so a pre-upgrade estama.db
    gets the run_log / daily_cast_status tables on first touch.
    """
    # code lives in the repo's app/ dir; APP is the data dir (redirected
    # by ESTAMA_VM_BASE in tests/staging)
    sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
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


# ------------------------------------------------------------------ jobs ----

CURRENT_RUN_ID: int | None = None  # run_log id of the active job
RUN_STARTED_AT: datetime | None = None  # naive JST; set right before the job


def run_scraper(args: list[str], timeout: int = 1800) -> None:
    env = dict(os.environ)
    if CURRENT_RUN_ID is not None:
        env["ESTAMA_RUN_ID"] = str(CURRENT_RUN_ID)  # scraper -> scrape_log join
    proc = subprocess.run(
        [str(VENV_PY), *args],
        cwd=APP, capture_output=True, text=True, timeout=timeout, env=env,
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    for line in tail:
        log(f"  scraper: {line}")
    if proc.returncode != 0:
        raise RuntimeError(f"scraper exited {proc.returncode}")


# --------------------------------------------------------------- pruning ----

BAN_AFTER_DAYS = 20   # observation span before a never-sold-out shop is banned
BAN_MIN_OBS_DAYS = 15  # minimum distinct observed days (coverage guard)
SHOPS_JSON = APP / "shops.json"
BANNED_JSON = APP / "banned_shops.json"


def _load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def shop_stats(con: sqlite3.Connection, shop_id: int) -> dict:
    first, last, days, total, sold = con.execute(
        """SELECT MIN(target_date), MAX(target_date),
                  COUNT(DISTINCT target_date), COUNT(*),
                  SUM(CASE WHEN status_norm='sold_out' THEN 1 ELSE 0 END)
           FROM snapshots WHERE shop_id=?""", (shop_id,)).fetchone()
    return {"first_seen": first, "last_seen": last, "obs_days": days or 0,
            "rows": total or 0, "sold_out": sold or 0}


def prune_candidates(db_path: Path, today_bd: str) -> list[dict]:
    """Shops observed for >= BAN_AFTER_DAYS with zero sold_out casts ever."""
    shops = _load_json(SHOPS_JSON, {"shops": []})["shops"]
    banned_ids = {b["shop_id"] for b in _load_json(BANNED_JSON, {"banned": []})["banned"]}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = []
    try:
        for shop in shops:
            sid = shop["shop_id"]
            if sid in banned_ids:
                continue
            st = shop_stats(con, sid)
            if not st["first_seen"]:
                continue  # no data at all: a fetch problem, not a verdict
            span = (datetime.fromisoformat(today_bd)
                    - datetime.fromisoformat(st["first_seen"])).days + 1
            if (span >= BAN_AFTER_DAYS and st["obs_days"] >= BAN_MIN_OBS_DAYS
                    and st["sold_out"] == 0):
                out.append({**shop, **st, "span_days": span})
    finally:
        con.close()
    return out


def apply_bans(candidates: list[dict], today_bd: str) -> None:
    shops_doc = _load_json(SHOPS_JSON, {"shops": []})
    banned_doc = _load_json(BANNED_JSON, {"banned": []})
    ids = {c["shop_id"] for c in candidates}
    shops_doc["shops"] = [s for s in shops_doc["shops"] if s["shop_id"] not in ids]
    for c in candidates:
        banned_doc["banned"].append({
            "shop_id": c["shop_id"], "name": c.get("name"), "banned_on": today_bd,
            "span_days": c["span_days"], "obs_days": c["obs_days"],
            "reason": "no sold_out cast in >=20 observed days",
        })
        log(f"BANNED shop {c['shop_id']} {c.get('name')}: observed "
            f"{c['obs_days']} days over {c['span_days']}d span, sold_out=0")
    SHOPS_JSON.write_text(json.dumps(shops_doc, ensure_ascii=False, indent=2))
    BANNED_JSON.write_text(json.dumps(banned_doc, ensure_ascii=False, indent=2))


def enforce_ban() -> list[int]:
    """Drop banned shops that a ranking sync may have re-added to shops.json."""
    banned_ids = {b["shop_id"] for b in _load_json(BANNED_JSON, {"banned": []})["banned"]}
    if not banned_ids:
        return []
    shops_doc = _load_json(SHOPS_JSON, {"shops": []})
    kept = [s for s in shops_doc["shops"] if s["shop_id"] not in banned_ids]
    dropped = [s["shop_id"] for s in shops_doc["shops"] if s["shop_id"] in banned_ids]
    if dropped:
        shops_doc["shops"] = kept
        SHOPS_JSON.write_text(json.dumps(shops_doc, ensure_ascii=False, indent=2))
        log(f"enforce_ban: re-removed banned shops {dropped}")
    return dropped


def job_prune() -> str:
    today_bd = expected_business_date(now_jst())
    candidates = prune_candidates(DB, today_bd)
    if candidates:
        apply_bans(candidates, today_bd)
        storage_put("shops.json", SHOPS_JSON)
        storage_put("banned_shops.json", BANNED_JSON)
    else:
        log("prune: no shops qualify for a ban")
    return f"banned={len(candidates)}"


def _archive_best_effort() -> None:
    """A failed archive must not block the live DB update.

    The archive is a point-in-time copy; the rows themselves stay in the
    DB, and a failed key is deleted on verification failure so the next
    run retries it."""
    try:
        maybe_archive(now_jst())
    except Exception as exc:
        log(f"archive failed (job continues): {type(exc).__name__}: {exc}")


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


def job_checkpoint() -> str:
    _pre_run_pull()
    _archive_best_effort()
    before = snapshot_count(DB)
    run_scraper(["estama_scraper.py", "--all-shops", "--import-db"])
    run_scraper(["estama_tracker.py", "rollup"])  # refresh daily_cast_status
    after = snapshot_count(DB)
    log(f"checkpoint: snapshots {before} -> {after} (+{after - before})")
    if after <= before:
        raise RuntimeError("checkpoint imported no new rows - refusing to upload")
    # Shrink before publish so the uploaded object (and the archive of
    # tomorrow) carry the bounded DB. The archive of the closed day has
    # already been written above.
    _db_retention_best_effort()
    return f"snapshots +{after - before} ({before} -> {after})"


# --------------------------------------------------------- publish ----
# What goes out after each job: the DB itself, the HTML dashboard the
# user reads on the data branch, and a webhook ping when something
# noteworthy happened. Both are best-effort: a broken notifier or a
# failed html write must not kill a good scrape.
DASHBOARD = APP / "dashboard.html"


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


def job_guard(max_age_min: float) -> int:
    """Exit 0 when the VM is keeping up (skip), 3 when it is not (run)."""
    payload = read_heartbeat()
    age = heartbeat_age_min(payload, now_jst())
    if age is None:
        log("guard: no usable heartbeat -> taking over")
        return 3
    if age <= max_age_min:
        log(f"guard: heartbeat {age:.0f} min old "
            f"(job={payload.get('job')}) -> VM is alive, skip")
        return 0
    log(f"guard: heartbeat {age:.0f} min old (> {max_age_min:.0f}) -> take over")
    return 3


def publish() -> None:
    upload_db()
    try:
        run_scraper(["estama_tracker.py", "dashboard", "--out",
                     str(DASHBOARD)])
        storage_put("dashboard.html", DASHBOARD)
        log("publish: dashboard.html uploaded")
    except Exception as exc:
        log(f"publish: dashboard skipped: {exc}")
    try:
        run_scraper(["estama_tracker.py", "notify"])
    except Exception as exc:
        log(f"publish: notify skipped: {exc}")


def job_ranking() -> str:
    _pre_run_pull()
    _archive_best_effort()
    run_scraper(["ranking_scraper.py", "--area", "all", "--import-db",
                 "--save-json", "--sync-watchlist",
                 "--sync-max-rank", "30"], timeout=600)
    run_scraper(["estama_tracker.py", "rollup"])  # refresh daily_cast_status
    enforce_ban()   # a sync must never resurrect a banned shop
    banned = job_prune()  # daily: ban shops w/ 20+ observed days, zero sell-outs
    # shops.json is part of the deliverable too
    storage_put("shops.json", APP / "shops.json")
    log("ranking: shops.json uploaded")
    return f"ranking sync done; {banned}"


class Job(NamedTuple):
    """One operational job.

    publishes: whether the job's output is pushed to the shared store.
    Checkpoint and ranking publish; prune only edits local state and
    never writes to the shared DB (its edits travel with the next
    publish).
    """

    run: Callable[[], str]
    publishes: bool


# The single place that knows what a job is. main() and the workflow
# validation both read this table; a job missing from it cannot be run
# from the CLI at all (which is the intended fail-closed behaviour).
JOBS: dict[str, Job] = {
    "checkpoint": Job(job_checkpoint, publishes=True),
    "ranking": Job(job_ranking, publishes=True),
    "prune": Job(job_prune, publishes=False),
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
        # Read-only: never opens the DB or writes run_log.
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
            return 3  # unknown state: taking over is the safe choice
    if job == "pull":
        # The DB does not exist yet, so run_log has nowhere to go.
        try:
            detail = job_pull()
        except Exception as exc:
            log(f"FAIL pull: {type(exc).__name__}: {exc}")
            return 1
        log(f"DONE pull: {detail}")
        return 0
    log(f"START {job}")
    global CURRENT_RUN_ID, RUN_STARTED_AT
    RUN_STARTED_AT = now_jst().replace(tzinfo=None)
    run_id = run_log_open(job)
    CURRENT_RUN_ID = run_id
    try:
        detail = JOBS[job].run()
    except Exception as exc:  # report, never crash the schedule silently
        log(f"FAIL {job}: {type(exc).__name__}: {exc}")
        run_log_close(run_id, "fail", f"{type(exc).__name__}: {exc}")
        # no publish: job_checkpoint's 'no new rows' rejection is also an
        # upload guard - a failed job must not replace the shared DB
        return 1
    finished_at = run_log_close(run_id, "ok", detail)
    # publish only AFTER run_log_close and only for jobs that ship
    # artifacts: the distributed DB's newest run row must show the
    # final status, not a stale 'running' (section_runlog flags a
    # running row older than 2h as suspected runner death)
    if JOBS[job].publishes:
        try:
            publish()
        except Exception as exc:  # upload failed - the artifact is stale
            # and nobody downstream can tell, so flip the row and exit
            # non-zero for cron even though the scrape itself succeeded
            log(f"publish failed after ok: {type(exc).__name__}: {exc}")
            run_log_close(run_id, "fail",
                          f"job ok, publish failed: {type(exc).__name__}: {exc}")
            return 1
        _retention_best_effort()
        try:
            write_heartbeat(job, detail, finished_at)
        except Exception as exc:
            # Liveness bookkeeping only: the artifact was already
            # published, so this must not turn a good run red.
            log(f"heartbeat failed (job ok): {type(exc).__name__}: {exc}")
    log(f"DONE {job}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
