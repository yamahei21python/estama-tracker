#!/usr/bin/env python3
"""estama-tracker runner (Muse VM).

Wraps the scrapers with the operational safeguards agreed 2026-10-26:

1. Single-instance lock (fcntl.flock) shared by checkpoint and ranking jobs,
   so a slow run can never overlap the next one and corrupt the SQLite DB.
2. Upload safety: the DB is verified (PRAGMA integrity_check + row counts)
   and snapshotted through the SQLite backup API *before* upload. The upload
   goes to a staging key first, its size is verified, and only then is it
   promoted over the live `estama.db` key - a bad file can never replace the
   last good one.
3. Business-date daily archive: before scraping, if the DB's latest
   target_date (the business date, which flips at ~05:00-06:00 JST) is older
   than the expected current business date, the finished business day is
   archived to `archive/estama-<business-date>.db` exactly once.

Storage backend: Cloudflare R2 via boto3 when ESTAMA_R2_* env vars are set;
otherwise a local mirror directory (BASE/r2_local/) so the whole pipeline is
testable before the bucket/token exist. Behaviour (staging -> verify ->
promote) is identical in both modes.

Usage:
    runner.py checkpoint    # scrape all shops (today only) + verify + upload
    runner.py ranking       # weekly rankings + watchlist sync + upload
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
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
STAGING_KEY = "estama.db.staging"
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


def storage_put(key: str, local_file: Path) -> None:
    if r2_configured():
        _s3().upload_file(str(local_file), os.environ["ESTAMA_R2_BUCKET"], key)
    else:
        dest = LOCAL_MIRROR / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_file, dest)


def storage_size(key: str) -> int | None:
    if r2_configured():
        try:
            head = _s3().head_object(Bucket=os.environ["ESTAMA_R2_BUCKET"], Key=key)
            return int(head["ContentLength"])
        except Exception:
            return None
    p = LOCAL_MIRROR / key
    return p.stat().st_size if p.exists() else None


def storage_copy(src_key: str, dst_key: str) -> None:
    if r2_configured():
        bucket = os.environ["ESTAMA_R2_BUCKET"]
        _s3().copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": src_key}, Key=dst_key)
    else:
        src, dst = LOCAL_MIRROR / src_key, LOCAL_MIRROR / dst_key
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def storage_delete(key: str) -> None:
    if r2_configured():
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


def db_business_date(path: Path) -> str | None:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT MAX(target_date) FROM snapshots").fetchone()
        return row[0] if row else None
    finally:
        con.close()


def snapshot_count(path: Path) -> int:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------- upload ----

def upload_db() -> dict:
    """Verify -> snapshot -> staged upload -> size check -> promote."""
    stats = verify_db(DB)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    snap = STAGING_DIR / f"estama-{now_jst():%Y%m%d-%H%M%S}.db"
    snapshot_copy(DB, snap)
    verify_db(snap)  # the copy itself must be intact before it can go live
    size = snap.stat().st_size
    storage_put(STAGING_KEY, snap)
    remote = storage_size(STAGING_KEY)
    if remote != size:
        raise IOError(f"staged upload size mismatch: local {size} vs remote {remote}")
    storage_copy(STAGING_KEY, LIVE_KEY)
    if storage_size(LIVE_KEY) != size:
        raise IOError("promoted object size mismatch")
    storage_delete(STAGING_KEY)
    snap.unlink(missing_ok=True)
    log(f"upload ok: {LIVE_KEY} {size} bytes, stats={stats}, backend={'R2' if r2_configured() else 'local-mirror'}")
    return stats


# --------------------------------------------------------------- archive ----

def expected_business_date(now: datetime) -> str:
    return (now - timedelta(hours=BUSINESS_DAY_FLIP_HOUR)).date().isoformat()


def maybe_archive(now: datetime) -> str | None:
    """Archive the just-finished business day once, before new data lands."""
    bd = db_business_date(DB)
    if not bd or bd >= expected_business_date(now):
        return None
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    if state.get("last_archived") == bd:
        return None
    key = f"archive/estama-{bd}.db"
    if not storage_exists(key):
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        snap = STAGING_DIR / f"archive-{bd}.db"
        snapshot_copy(DB, snap)
        verify_db(snap)
        storage_put(key, snap)
        if storage_size(key) != snap.stat().st_size:
            raise IOError(f"archive upload size mismatch for {key}")
        snap.unlink(missing_ok=True)
    state["last_archived"] = bd
    STATE_FILE.write_text(json.dumps(state))
    log(f"archive ok: {key} (business date {bd} closed)")
    return key


# ------------------------------------------------------------------ jobs ----

def run_scraper(args: list[str], timeout: int = 1800) -> None:
    proc = subprocess.run(
        [str(VENV_PY), *args],
        cwd=APP, capture_output=True, text=True, timeout=timeout,
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


def job_prune() -> None:
    today_bd = expected_business_date(now_jst())
    candidates = prune_candidates(DB, today_bd)
    if candidates:
        apply_bans(candidates, today_bd)
        storage_put("shops.json", SHOPS_JSON)
        storage_put("banned_shops.json", BANNED_JSON)
    else:
        log("prune: no shops qualify for a ban")


def job_checkpoint() -> None:
    maybe_archive(now_jst())
    before = snapshot_count(DB)
    run_scraper(["estama_scraper.py", "--all-shops", "--import-db"])
    after = snapshot_count(DB)
    log(f"checkpoint: snapshots {before} -> {after} (+{after - before})")
    if after <= before:
        raise RuntimeError("checkpoint imported no new rows - refusing to upload")
    upload_db()


def job_ranking() -> None:
    maybe_archive(now_jst())
    run_scraper(["ranking_scraper.py", "--area", "all", "--import-db",
                 "--save-json", "--sync-watchlist"], timeout=600)
    enforce_ban()   # a sync must never resurrect a banned shop
    job_prune()     # daily: ban shops with 20+ observed days and zero sell-outs
    upload_db()
    # shops.json is part of the deliverable too
    storage_put("shops.json", APP / "shops.json")
    log("ranking: shops.json uploaded")


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
        return 0
    job = sys.argv[1]
    log(f"START {job}")
    try:
        {"checkpoint": job_checkpoint, "ranking": job_ranking,
         "prune": job_prune}[job]()
    except Exception as exc:  # report, never crash the schedule silently
        log(f"FAIL {job}: {type(exc).__name__}: {exc}")
        return 1
    log(f"DONE {job}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
