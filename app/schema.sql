-- estama-tracker schema (SQLite)
-- Purpose: track sold-out casts on Estama (estama.jp), Osaka watchlist.
-- Supports time-series snapshots (hourly checkpoints at :30, 24 per day)
-- to analyze sold-out speed and work hours.

CREATE TABLE IF NOT EXISTS shops (
    shop_id      INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    area         TEXT,
    store_status TEXT,
    source_url   TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id      INTEGER NOT NULL REFERENCES shops (shop_id),
    cast_name    TEXT NOT NULL,
    cast_id      INTEGER,
    age          INTEGER,
    shift        TEXT,
    shift_start  TEXT,    -- e.g. "12:00"
    shift_end    TEXT,    -- e.g. "20:00"
    work_minutes INTEGER, -- e.g. 480 (8 hours)
    work_hours   REAL,    -- e.g. 8.0
    status_raw   TEXT,
    status_norm  TEXT NOT NULL CHECK (
        status_norm IN ('sold_out', 'closed', 'low', 'available', 'unknown', 'dummy')
    ),
    slot_pattern TEXT,
    target_date  TEXT NOT NULL,
    fetched_at   TEXT,
    fetched_time TEXT,    -- e.g. "09:00", "11:00", "17:00"
    -- Unique per shop, cast, date, and fetch checkpoint time
    UNIQUE (shop_id, cast_name, target_date, fetched_time)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_cast_date
    ON snapshots (shop_id, cast_name, target_date);
CREATE INDEX IF NOT EXISTS idx_snapshots_status_date
    ON snapshots (status_norm, target_date);
CREATE INDEX IF NOT EXISTS idx_snapshots_time
    ON snapshots (target_date, fetched_time);
CREATE INDEX IF NOT EXISTS idx_snapshots_work_hours
    ON snapshots (work_hours);

-- Weekly / Monthly Popular Cast Rankings
CREATE TABLE IF NOT EXISTS cast_rankings (
    ranking_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    area_code    TEXT NOT NULL,         -- e.g. "2701", "2700"
    area_name    TEXT NOT NULL,         -- e.g. "梅田・大阪駅", "大阪府総合"
    period_type  TEXT NOT NULL,         -- e.g. "weekly"
    rank         INTEGER,               -- 1〜30
    is_pr        BOOLEAN DEFAULT 0,     -- 1 if PR slot
    cast_id      INTEGER NOT NULL,
    cast_name    TEXT NOT NULL,
    age          INTEGER,
    height       INTEGER,
    cup          TEXT,
    shop_id      INTEGER NOT NULL,
    shop_name    TEXT NOT NULL,
    fetched_date TEXT NOT NULL,         -- YYYY-MM-DD
    fetched_at   TEXT NOT NULL,
    UNIQUE (area_code, period_type, rank, is_pr, fetched_date)
);

CREATE INDEX IF NOT EXISTS idx_rankings_cast_date
    ON cast_rankings (cast_id, fetched_date);
CREATE INDEX IF NOT EXISTS idx_rankings_shop_date
    ON cast_rankings (shop_id, fetched_date);
CREATE INDEX IF NOT EXISTS idx_rankings_area_date
    ON cast_rankings (area_code, fetched_date);
