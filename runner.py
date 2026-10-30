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

import base64
import fcntl
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
BASE = Path(os.environ.get("ESTAMA_VM_BASE", Path(__file__).resolve().parent))
APP = BASE / "app"
DB = APP / "estama.db"
VENV_PY = BASE / "venv" / "bin" / "python"
LOCK_FILE = BASE / "runner.lock"
STATE_FILE = BASE / "archive_state.json"
LOG_DIR = BASE / "logs"
LOCAL_MIRROR = BASE / "r2_local"
STAGING_DIR = BASE / "staging"

LIVE_KEY = "estama.db"
BUSINESS_DAY_FLIP_HOUR = 6  # site business date flips ~05:00-06:00; use 06:00


def now_jst() -> datetime:
    return datetime.now(JST)


def log(msg: str) -> None:
    ts = now_jst().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / f"runner-{now_jst():%Y-%m-%d}.log", "a") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------- storage ---

def r2_configured() -> bool:
    return all(
        os.environ.get(k)
        for k in ("ESTAMA_R2_ENDPOINT", "ESTAMA_R2_BUCKET",
                  "ESTAMA_R2_ACCESS_KEY_ID", "ESTAMA_R2_SECRET_ACCESS_KEY")
    )


def _s3():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["ESTAMA_R2_ENDPOINT"],
        aws_access_key_id=os.environ["ESTAMA_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["ESTAMA_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


STORAGE_CONFIG = BASE / "storage.json"


def storage_config() -> dict:
    if STORAGE_CONFIG.exists():
        return json.loads(STORAGE_CONFIG.read_text())
    return {}


def backend_name() -> str:
    if storage_config().get("backend") == "github":
        return "github"
    if r2_configured():
        return "R2"
    return "local-mirror"


# ------------------------------------------------------- GitHub backend ----
# Objects live as files on the `data` branch of the code repo. DB payloads
# are gzip-compressed (deterministic, mtime=0) so blob SHAs are reproducible:
# verification compares the remote blob SHA against the locally computed
# git blob SHA of the (compressed) bytes - stronger than a size check.
# Auth: the custom.github-cli connector surrogate, swapped for the real PAT
# on egress to api.github.com (same mechanics as the connector helper).

GH_CONNECTOR = "custom.github-cli"
GH_API = "https://api.github.com"
GH_CRED_LIB = "/opt/muse/skills/custom-connector-setup/bin"


def gh_path(key: str) -> str:
    if key.endswith(".db"):
        return key + ".gz"
    return key


def gh_blob_sha(data: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def gh_payload(local_file: Path, key: str) -> bytes:
    data = local_file.read_bytes()
    if gh_path(key) != key:
        data = gzip.compress(data, mtime=0)
    return data


def _gh_api(method: str, path: str, body: dict | None = None,
            timeout: int = 180) -> dict:
    sys.path.insert(0, GH_CRED_LIB)
    from dynamic_credentials import add_surrogate_to_request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GH_API + path, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    add_surrogate_to_request(req, GH_CONNECTOR, allowed_hosts=["api.github.com"])
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def _gh_repo_branch() -> tuple[str, str]:
    cfg = storage_config()
    return cfg.get("repo", "yamahei21python/estama-tracker"), cfg.get("branch", "data")


def _gh_head() -> tuple[str, str]:
    repo, branch = _gh_repo_branch()
    ref = _gh_api("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    commit_sha = ref["object"]["sha"]
    commit = _gh_api("GET", f"/repos/{repo}/git/commits/{commit_sha}")
    return commit_sha, commit["tree"]["sha"]


def _gh_entries() -> dict:
    """path -> tree entry for every blob on the data branch."""
    repo, _ = _gh_repo_branch()
    _, tree_sha = _gh_head()
    out = _gh_api("GET", f"/repos/{repo}/git/trees/{tree_sha}?recursive=1")
    return {e["path"]: e for e in out.get("tree", []) if e["type"] == "blob"}


def _gh_commit(entries: list[dict], message: str) -> None:
    repo, branch = _gh_repo_branch()
    head_sha, tree_sha = _gh_head()
    tree = _gh_api("POST", f"/repos/{repo}/git/trees",
                   {"base_tree": tree_sha, "tree": entries})
    commit = _gh_api("POST", f"/repos/{repo}/git/commits",
                     {"message": message, "tree": tree["sha"],
                      "parents": [head_sha]})
    _gh_api("PATCH", f"/repos/{repo}/git/refs/heads/{branch}",
            {"sha": commit["sha"]})


def _gh_put(key: str, local_file: Path) -> None:
    repo, _ = _gh_repo_branch()
    data = gh_payload(local_file, key)
    blob = _gh_api("POST", f"/repos/{repo}/git/blobs",
                   {"content": base64.b64encode(data).decode(),
                    "encoding": "base64"})
    if blob["sha"] != gh_blob_sha(data):
        raise IOError(f"github blob sha mismatch for {key}")
    _gh_commit([{"path": gh_path(key), "mode": "100644", "type": "blob",
                 "sha": blob["sha"]}], f"data: update {key}")


def _gh_size(key: str) -> int | None:
    entry = _gh_entries().get(gh_path(key))
    return int(entry["size"]) if entry else None


def _gh_verify(key: str, local_file: Path) -> bool:
    entry = _gh_entries().get(gh_path(key))
    return bool(entry) and entry["sha"] == gh_blob_sha(gh_payload(local_file, key))


def _gh_live_blob(key: str) -> str | None:
    """Blob SHA currently stored at key (None when missing).

    API errors propagate on purpose: upload_db must abort before
    writing when it cannot capture a rollback target."""
    entry = _gh_entries().get(gh_path(key))
    return entry["sha"] if entry else None


def _gh_restore(key: str, blob_sha: str) -> None:
    """Point key back at a previously verified blob in one commit."""
    _gh_commit([{"path": gh_path(key), "mode": "100644", "type": "blob",
                 "sha": blob_sha}],
               f"data: restore {key} (post-write verify failed)")


def _gh_delete(key: str) -> None:
    _gh_commit([{"path": gh_path(key), "mode": "100644", "type": "blob",
                 "sha": None}], f"data: delete {key}")


# ------------------------------------------------------------ dispatch -----

def storage_put(key: str, local_file: Path) -> None:
    b = backend_name()
    if b == "github":
        _gh_put(key, local_file)
    elif b == "R2":
        _s3().upload_file(str(local_file), os.environ["ESTAMA_R2_BUCKET"], key)
    else:
        dest = LOCAL_MIRROR / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_file, dest)


def storage_size(key: str) -> int | None:
    b = backend_name()
    if b == "github":
        try:
            return _gh_size(key)
        except Exception:
            return None
    if b == "R2":
        try:
            head = _s3().head_object(Bucket=os.environ["ESTAMA_R2_BUCKET"], Key=key)
            return int(head["ContentLength"])
        except Exception:
            return None
    p = LOCAL_MIRROR / key
    return p.stat().st_size if p.exists() else None


def storage_verify(key: str, local_file: Path) -> bool:
    """True when the stored object provably matches the local file."""
    if backend_name() == "github":
        try:
            return _gh_verify(key, local_file)
        except Exception:
            return False
    return storage_size(key) == local_file.stat().st_size


def storage_delete(key: str) -> None:
    b = backend_name()
    if b == "github":
        _gh_delete(key)
    elif b == "R2":
        _s3().delete_object(Bucket=os.environ["ESTAMA_R2_BUCKET"], Key=key)
    else:
        (LOCAL_MIRROR / key).unlink(missing_ok=True)


def storage_exists(key: str) -> bool:
    return storage_size(key) is not None


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
        prev_blob = (_gh_live_blob(LIVE_KEY)
                     if backend_name() == "github" else None)
        storage_put(LIVE_KEY, snap)
        if not storage_verify(LIVE_KEY, snap):
            storage_put(LIVE_KEY, snap)  # retry once from the good copy
        if not storage_verify(LIVE_KEY, snap):
            if backend_name() == "github":
                if prev_blob:
                    _gh_restore(LIVE_KEY, prev_blob)
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
    return (now - timedelta(hours=BUSINESS_DAY_FLIP_HOUR)).date().isoformat()


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
    log(f"DONE {job}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
