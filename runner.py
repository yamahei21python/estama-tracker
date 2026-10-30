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
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(os.environ.get("ESTAMA_VM_BASE", Path(__file__).resolve().parent))
APP = BASE / "app"
DB = APP / "estama.db"
VENV_PY = BASE / "venv" / "bin" / "python"
LOCK_FILE = BASE / "runner.lock"
STATE_FILE = BASE / "archive_state.json"
LOG_DIR = BASE / "logs"
STAGING_DIR = BASE / "staging"

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(CODE_DIR / "app"))
from common import business_date, now_jst  # noqa: E402
from storage import (  # noqa: E402
    backend_name, gh_live_blob, gh_restore, storage_config, storage_delete,
    storage_exists, storage_list, storage_put, storage_verify,
)

LIVE_KEY = "estama.db"


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


# ---------------------------------------------------------------- upload ----

def upload_db() -> dict:
    """Verify -> snapshot -> one direct live write -> verify -> restore.

    The DB goes straight to LIVE_KEY as a single data-branch commit.
    (The old staging -> promote -> delete dance cost three commits per
    checkpoint.) The written blob is verified against the locally
    verified snapshot; only a mismatch re-uploads it once, and if that
    also fails the previous live blob is restored so the branch never
    keeps a corrupt DB.
    """
    stats = verify_db(DB)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    snap = STAGING_DIR / f"estama-{now_jst():%Y%m%d-%H%M%S}.db"
    size = 0
    try:
        snapshot_copy(DB, snap)
        verify_db(snap)  # the copy itself must be intact before it can go live
        size = snap.stat().st_size
        prev_blob = (gh_live_blob(LIVE_KEY)
                     if backend_name() == "github" else None)
        storage_put(LIVE_KEY, snap)
        if not storage_verify(LIVE_KEY, snap):
            storage_put(LIVE_KEY, snap)  # retry once from the good copy
        if not storage_verify(LIVE_KEY, snap):
            if backend_name() == "github":
                if prev_blob:
                    gh_restore(LIVE_KEY, prev_blob)
                    log(f"upload: verified live write failed, restored {LIVE_KEY}")
                else:
                    storage_delete(LIVE_KEY)
                    log(f"upload: verified live write failed, deleted {LIVE_KEY}")
            raise IOError("live object verification failed after restore")
    finally:
        snap.unlink(missing_ok=True)
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
    cfg = storage_config().get("archive_retention") or {}
    days = cfg.get("days")
    if not days:
        return []  # unset: no deletion
    stale = stale_archive_keys(
        storage_list("archive/"), now.date(), int(days),
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


def run_log_close(run_id: int | None, status: str, detail: str | None = None) -> None:
    if run_id is None:
        return
    try:
        con = _run_db()
        con.execute(
            "UPDATE run_log SET finished_at = ?, status = ?, detail = ?"
            " WHERE run_id = ?",
            (_run_ts(), status, detail, run_id))
        con.commit()
        con.close()
    except Exception as exc:
        log(f"run_log close failed: {exc}")


def run_log_mark(job: str, status: str, detail: str | None = None) -> None:
    """One-shot completed row (e.g. a lock SKIP that never ran)."""
    run_id = run_log_open(job)
    run_log_close(run_id, status, detail)


# ------------------------------------------------------------------ jobs ----

CURRENT_RUN_ID: int | None = None  # run_log id of the active job


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


def job_checkpoint() -> str:
    _archive_best_effort()
    before = snapshot_count(DB)
    run_scraper(["estama_scraper.py", "--all-shops", "--import-db"])
    run_scraper(["estama_tracker.py", "rollup"])  # refresh daily_cast_status
    after = snapshot_count(DB)
    log(f"checkpoint: snapshots {before} -> {after} (+{after - before})")
    if after <= before:
        raise RuntimeError("checkpoint imported no new rows - refusing to upload")
    return f"snapshots +{after - before} ({before} -> {after})"


# --------------------------------------------------------- publish ----
# What goes out after each job: the DB itself, the HTML dashboard the
# user reads on the data branch, and a webhook ping when something
# noteworthy happened. Both are best-effort: a broken notifier or a
# failed html write must not kill a good scrape.
DASHBOARD = APP / "dashboard.html"


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


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("checkpoint", "ranking", "prune"):
        print(__doc__)
        return 2
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"SKIP {sys.argv[1]}: another run holds the lock")
        run_log_mark(sys.argv[1], "skip", "another run holds the lock")
        return 0
    job = sys.argv[1]
    log(f"START {job}")
    run_id = run_log_open(job)
    global CURRENT_RUN_ID
    CURRENT_RUN_ID = run_id
    try:
        detail = {"checkpoint": job_checkpoint, "ranking": job_ranking,
                  "prune": job_prune}[job]()
    except Exception as exc:  # report, never crash the schedule silently
        log(f"FAIL {job}: {type(exc).__name__}: {exc}")
        run_log_close(run_id, "fail", f"{type(exc).__name__}: {exc}")
        # no publish: job_checkpoint's 'no new rows' rejection is also an
        # upload guard - a failed job must not replace the shared DB
        return 1
    run_log_close(run_id, "ok", detail)
    # publish only AFTER run_log_close and only for jobs that ship
    # artifacts: the distributed DB's newest run row must show the
    # final status, not a stale 'running' (section_runlog flags a
    # running row older than 2h as suspected runner death)
    if job in ("checkpoint", "ranking"):
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
    log(f"DONE {job}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
