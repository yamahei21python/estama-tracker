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
import os
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
        _migrate_status_norm_dummy(conn)
        _migrate_reclassify_decoy(conn)
    return conn


def _migrate_reclassify_decoy(conn) -> None:
    """Re-label legacy '全コマ×(事前予約不可/おとり候補)' rows as dummy.

    They were stored as 'unknown' (2026-10-27 rows) before the scraper
    learned that no-text + all-crossed = unbookable/decoy. Pure
    re-classification of the same raw text; idempotent.
    """
    conn.execute(
        """UPDATE snapshots SET status_norm = 'dummy'
           WHERE status_norm = 'unknown'
             AND status_raw = '全コマ×(事前予約不可/おとり候補)'"""
    )
    conn.commit()


def parse_clock(hhmm):
    """Parse 'H:MM'/'HH:MM' (+24h+ over-hours forms) to minutes.

    '9:00' -> 540, '09:00' -> 540, '24:00' -> 1440, '29:00' -> 1740.
    String comparison of raw shift values is wrong ('9:00' > '23:00'
    lexicographically); compare parse_clock() ints instead.
    Returns None for None/empty/malformed input.
    """
    if not hhmm:
        return None
    parts = str(hhmm).strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if hour < 0 or not 0 <= minute <= 59:
        return None  # negative / '9:99'-style minutes are malformed
    return hour * 60 + minute


def _migrate_status_norm_dummy(conn) -> None:
    """Rebuild snapshots if its CHECK lacks 'dummy' (TEL-only casts).

    SQLite cannot ALTER a CHECK constraint; recreate table + indexes
    and copy rows. Idempotent: no-op once 'dummy' is in the SQL text.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='snapshots'"
    ).fetchone()
    if not row or not row[0] or "dummy" in row[0]:
        return
    conn.executescript(
        """
        CREATE TABLE snapshots_new (
            snapshot_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id      INTEGER NOT NULL REFERENCES shops (shop_id),
            cast_name    TEXT NOT NULL,
            cast_id      INTEGER,
            age          INTEGER,
            shift        TEXT,
            shift_start  TEXT,
            shift_end    TEXT,
            work_minutes INTEGER,
            work_hours   REAL,
            status_raw   TEXT,
            status_norm  TEXT NOT NULL CHECK (
                status_norm IN ('sold_out', 'closed', 'low', 'available', 'unknown', 'dummy')
            ),
            slot_pattern TEXT,
            target_date  TEXT NOT NULL,
            fetched_at   TEXT,
            fetched_time TEXT,
            UNIQUE (shop_id, cast_name, target_date, fetched_time)
        );
        INSERT INTO snapshots_new SELECT * FROM snapshots;
        DROP TABLE snapshots;
        ALTER TABLE snapshots_new RENAME TO snapshots;
        CREATE INDEX IF NOT EXISTS idx_snapshots_cast_date
            ON snapshots (shop_id, cast_name, target_date);
        CREATE INDEX IF NOT EXISTS idx_snapshots_status_date
            ON snapshots (status_norm, target_date);
        CREATE INDEX IF NOT EXISTS idx_snapshots_time
            ON snapshots (target_date, fetched_time);
        CREATE INDEX IF NOT EXISTS idx_snapshots_work_hours
            ON snapshots (work_hours);
        """
    )
    conn.commit()


def load_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def save_json(path, obj) -> None:
    Path(path).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def log_scrape(db_path, shop_id, status, rows=0, detail=None) -> None:
    """Append one per-shop scrape outcome to scrape_log.

    Best effort: a logging failure never breaks the scrape. run_id is
    picked up from the ESTAMA_RUN_ID env var the runner exports when it
    launches the scraper, so the row joins back to its run_log entry.
    """
    try:
        run_id = os.environ.get("ESTAMA_RUN_ID")
        conn = connect_db(db_path)
        conn.execute(
            "INSERT INTO scrape_log (run_id, shop_id, fetched_at,"
            " status, rows, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (int(run_id) if run_id else None, shop_id,
             now_jst().strftime("%Y-%m-%d %H:%M:%S") + " JST",
             status, rows, detail))
        conn.commit()
        conn.close()
    except Exception:
        pass


def normalize_status_text(raw) -> str:
    """Map a therapist-level status string to a normalized status.

    Order matters: sold_out is checked before 'closed', because the
    wording for sold-out contains neither '受付終了' nor '残りわずか'.
    '受付終了' / '本日受付終了' is 'closed', explicitly NOT sold_out.
    'TELのみ' (scraper label for all-TEL columns) is 'dummy'.
    Anything unrecognised (including '時間のみ' and empty) is 'unknown'.
    Slot symbols are never consulted here.
    """
    if raw is None:
        return "unknown"
    text = str(raw)
    if "ご好評につき本日完売" in text or text.strip() == "本日完売":
        return "sold_out"
    if "TELのみ" in text:
        return "dummy"
    if "受付終了" in text:
        return "closed"
    if "残りわずか" in text:
        return "low"
    if "今すぐご案内" in text or "ご予約受付中" in text or "以内でご案内" in text:
        return "available"
    return "unknown"
