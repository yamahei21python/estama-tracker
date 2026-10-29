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

-- Daily rollup of the R3 effective final state, one row per cast-day.
-- Written by `estama_tracker.py rollup` (idempotent rebuild from
-- snapshots; the runner calls it after every import). Makes
-- "usually sold out" history, sold-out streaks and trend queries
-- cheap: no window function needed per lookup.
CREATE TABLE IF NOT EXISTS daily_cast_status (
    shop_id     INTEGER NOT NULL,
    cast_key    TEXT NOT NULL,     -- CAST(COALESCE(cast_id, cast_name) AS TEXT)
    cast_id     INTEGER,
    cast_name   TEXT NOT NULL,
    target_date TEXT NOT NULL,
    status_norm TEXT NOT NULL,
    status_raw  TEXT,
    work_hours  REAL,
    age         INTEGER,
    fetched_at  TEXT,              -- timestamp of the winning observation
    first_sold_out_at TEXT,        -- earliest sold_out fetched_at that day (NULL if never)
    was_low     INTEGER NOT NULL DEFAULT 0,  -- 'low' observed before the sold_out (or at all, if never)
    PRIMARY KEY (shop_id, cast_key, target_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_status_date
    ON daily_cast_status (target_date, status_norm);
CREATE INDEX IF NOT EXISTS idx_daily_status_cast
    ON daily_cast_status (cast_key, target_date);

-- Runner job history (checkpoint / ranking / prune), one row per run.
-- Written by runner.py so scrape failures and skips are queryable
-- from the DB itself instead of only the runner log files.
CREATE TABLE IF NOT EXISTS run_log (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,     -- checkpoint | ranking | prune
    started_at  TEXT NOT NULL,     -- YYYY-MM-DD HH:MM:SS JST
    finished_at TEXT,
    status      TEXT NOT NULL,     -- running | ok | fail | skip
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_log_started
    ON run_log (started_at);

-- Per-shop scrape audit inside a checkpoint run. One row per shop per
-- scrape attempt: 'ok' (rows>0), 'empty' (fetch worked, 0 cast rows) and
-- 'fail' (exception). Without it a failed shop is indistinguishable from
-- a shop with no casts on duty.
CREATE TABLE IF NOT EXISTS scrape_log (
    log_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER,                -- run_log.run_id when launched by runner
    shop_id    INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS JST'
    status     TEXT NOT NULL,          -- ok | empty | fail
    rows       INTEGER NOT NULL DEFAULT 0,
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_scrape_log_shop
    ON scrape_log (shop_id, fetched_at);
CREATE INDEX IF NOT EXISTS idx_scrape_log_status
    ON scrape_log (status, fetched_at);

-- Per-day evaluation of the 'low -> sold_out' forecast: of the casts
-- that showed 'low' during the day, how many ended up sold_out.
-- Written by `estama_tracker.py rollup` (after daily_cast_status so
-- was_low already exists); report section 4 prints the trend and the
-- cumulative precision, so the forecast's reliability is measurable
-- instead of assumed.
CREATE TABLE IF NOT EXISTS forecast_eval (
    target_date TEXT PRIMARY KEY,
    candidates  INTEGER NOT NULL,  -- cast-days with was_low = 1
    hits        INTEGER NOT NULL,  -- ... that ended the day sold_out
    evaluated_at TEXT NOT NULL
);

-- User-managed favorite casts: 'watch add/remove/list' CLI writes
-- here so the list survives through the DB itself (the data branch
-- carries the DB, so favorites follow it like everything else).
CREATE TABLE IF NOT EXISTS cast_watch (
    cast_key  TEXT NOT NULL,       -- CAST(COALESCE(cast_id, cast_name) AS TEXT)
    shop_id   INTEGER NOT NULL,
    cast_name TEXT NOT NULL,
    added_at  TEXT NOT NULL,
    note      TEXT,
    PRIMARY KEY (shop_id, cast_key)
);
