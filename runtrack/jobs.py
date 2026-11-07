"""Operational jobs and publication."""
from __future__ import annotations
from runtrack.env import APP, DB, DASHBOARD, SHOPS_JSON, log
from storage import storage_put
from runtrack.db import snapshot_count
from runtrack.archive import (expected_business_date, maybe_archive,
                              _retention_best_effort, _db_retention_best_effort)
from runtrack.storage_ops import _pull_if_remote_newer

def _pre_run_pull() -> None:
    if _pull_if_remote_newer():
        from runtrack.env import log
        log("pre-run: pulled remote DB before the run")
from runtrack.ban import enforce_ban, apply_bans, prune_candidates
from runtrack.scraper import run_scraper
from runtrack.storage_ops import upload_db, job_pull as _job_pull
from common import now_jst

def _archive_best_effort():
    try:
        maybe_archive(now_jst())
    except Exception as exc:
        log(f"archive failed (job continues): {type(exc).__name__}: {exc}")


def job_pull() -> str:
    return _job_pull()

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
