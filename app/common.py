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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

# Bumped by _migrate_schema; stored in PRAGMA user_version.
SCHEMA_VERSION = 1

# The site flips its business date around 05:00-06:00; everything newer
# than 06:00 JST belongs to that calendar date's business day.
BUSINESS_DAY_FLIP_HOUR = 6

# Row-level chronological sort key: full fetched_at when present, an
# HH:MM-only fallback for legacy rows (day unknown, so it sorts behind
# full-timestamp rows). Python twin: chrono_key().
CHRONO_KEY_SQL = (
    "COALESCE(fetched_at, '9999-12-31 ' || IFNULL(fetched_time, '00:00'))")


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


def business_date(now: datetime | None = None) -> str:
    """YYYY-MM-DD of the business day `now` belongs to (flip 06:00)."""
    return ((now or now_jst()) - timedelta(hours=BUSINESS_DAY_FLIP_HOUR)
            ).date().isoformat()


def business_day_start(date: str, tz=None) -> datetime:
    """Start timestamp (06:00) of a business date, in `tz` (default JST)."""
    return datetime.fromisoformat(
        f"{date}T{BUSINESS_DAY_FLIP_HOUR:02d}:00:00").replace(
            tzinfo=tz or JST)


def chrono_key(fetched_at, fetched_time=None) -> str:
    """Python twin of CHRONO_KEY_SQL (keep the two in sync)."""
    if fetched_at:
        return str(fetched_at)
    return "9999-12-31 " + (fetched_time or "00:00")


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
        _migrate_schema(conn)
    return conn


def _migrate_schema(conn) -> None:
    """Ordered migrations, versioned via PRAGMA user_version.

    schema.sql only adds what is missing (CREATE ... IF NOT EXISTS), so
    it always runs first; the migrations here cover changes that cannot
    be expressed additively (dropped indexes). Idempotent: a DB already
    at SCHEMA_VERSION is untouched, and every DB created before this
    mechanism is at version 0 on its first connect.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        # v1: retire indexes keyed on pre-fetched_at columns. All
        # time-series reads order by fetched_at (idx_snapshots_date_at)
        # and nothing filters on work_hours, so both only cost write
        # time. The UNIQUE key stays as-is: fetched_time is still the
        # checkpoint label the scraper dedupes on.
        conn.execute("DROP INDEX IF EXISTS idx_snapshots_time")
        conn.execute("DROP INDEX IF EXISTS idx_snapshots_work_hours")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()


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
        CREATE INDEX IF NOT EXISTS idx_snapshots_castid_date
            ON snapshots (shop_id, cast_id, target_date);
        CREATE INDEX IF NOT EXISTS idx_snapshots_status_date
            ON snapshots (status_norm, target_date);
        CREATE INDEX IF NOT EXISTS idx_snapshots_date_at
            ON snapshots (target_date, fetched_at);
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


def notify_webhook(text) -> bool:
    """POST a notification to the webhook in ESTAMA_NOTIFY_WEBHOOK.

    Payload carries both 'text' (Slack incoming-webhook / LINE-style
    bots that accept a generic field) and 'content' (Discord), so one
    URL works for Slack, Discord and most notifier endpoints. urllib
    only, best effort: returns True on 2xx, False otherwise.
    """
    url = os.environ.get("ESTAMA_NOTIFY_WEBHOOK")
    if not url:
        return False
    try:
        import urllib.request
        body = json.dumps(
            {"text": text, "content": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


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
