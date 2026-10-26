"""Shared building blocks for the estama-tracker tools.

Everything the scrapers, the tracker and the runner used to copy-paste
lives here exactly once:

- canonical paths (DB, schema, shops.json) derived from this file
- the browser User-Agent estama.jp requires (its ELB rejects the
  default python-requests UA with 403)
- requests session construction (requests is imported lazily, so this
  module stays importable with the standard library alone)
- SQLite connection setup (foreign keys on + schema applied)
- JSON load/save with the project's formatting (ensure_ascii=False,
  indent=2, UTF-8)
- the therapist-status text mapping shared by the live scraper and
  the JSON-import tracker

Sold-out is decided ONLY from the status text, never from slot
symbols (○/×/─); slot-based derivation lives in the scraper and only
ever yields available/low/closed/unknown, never sold_out.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def now_jst() -> datetime:
    """Current time in Asia/Tokyo.

    Every timestamp stored in the DB (fetched_at / fetched_time /
    fetched_date) must come from here, never from a naive
    datetime.now(): the VM clock is UTC, and naive stamps silently
    stored UTC values under a 'JST' label until 2026-10-26 (and a
    naive fetched_date could be a day behind in the early JST
    morning, e.g. the 06:15 ranking run).
    """
    return datetime.now(JST)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "estama.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"
SHOPS_JSON_PATH = BASE_DIR / "shops.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def make_session(referer: str | None = None):
    """A requests.Session with the browser UA estama.jp requires."""
    import requests  # lazy: keep this module stdlib-only at import time

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    if referer:
        session.headers.update({"Referer": referer})
    return session


def connect_db(db_path=None, apply_schema: bool = True) -> sqlite3.Connection:
    """Open the tracker DB with foreign keys on and the schema applied."""
    conn = sqlite3.connect(db_path if db_path is not None else DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    if apply_schema and SCHEMA_PATH.exists():
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def load_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(path, obj) -> None:
    Path(path).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_status_text(raw) -> str:
    """Map a therapist-level status string to a normalized status.

    Order matters: sold_out is checked before 'closed', because the
    wording for sold-out contains neither '受付終了' nor '残りわずか'.
    '受付終了' / '本日受付終了' is 'closed', explicitly NOT sold_out.
    Anything unrecognised (including '時間のみ' and empty) is 'unknown'.
    Slot symbols are never consulted here.
    """
    if raw is None:
        return "unknown"
    text = str(raw)
    if "ご好評につき本日完売" in text or text.strip() == "本日完売":
        return "sold_out"
    if "受付終了" in text:
        return "closed"
    if "残りわずか" in text:
        return "low"
    if "今すぐご案内" in text or "ご予約受付中" in text or "以内でご案内" in text:
        return "available"
    return "unknown"
