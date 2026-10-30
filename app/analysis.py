"""Alert logic and the daily rollup (data layer).

usually_sold_out_alert decides from R3 day finals; alert_evidence_lines
attaches display-only evidence (a broken source DB never changes the
alert); rollup_daily materializes the finals into daily_cast_status so
history lookups are a table read instead of a window pass.
"""
import sqlite3

import evidence as evidence_mod
from common import now_jst
from queries import _final_sql





def usually_sold_out_alert(conn, target_date, min_days=3, threshold=0.7):
    """Casts usually sold out but available/low on the target date.

    History is counted in DAYS, not snapshot rows: a cast's status for
    a day is that day's R3 EFFECTIVE final state (latest observation,
    walking back over the end-of-day closed reset - see _final_sql).
    So end-of-shift 'closed' no longer hides a sold-out day, and
    intraday checkpoints can never inflate the history. A cast qualifies
    when it was observed on >= min_days distinct days before the
    target date, its sold_out rate over those DAYS is >= threshold,
    and its target-date effective state is 'available' or 'low'.
    Returns (alerts, history_casts_considered).
    """
    history = conn.execute(
        f"""
        WITH final AS ({_final_sql('target_date < ?')})
        SELECT shop_id, CAST(COALESCE(cast_id, cast_name) AS TEXT) AS ckey,
               COUNT(*) AS n_days,
               SUM(CASE WHEN status_norm = 'sold_out' THEN 1 ELSE 0 END) AS n_sold_days
        FROM final
        GROUP BY shop_id, ckey HAVING COUNT(*) >= ?""",
        (target_date, min_days),
    ).fetchall()
    considered = len(history)
    usually = {
        (shop_id, ckey): (n, n_sold)
        for shop_id, ckey, n, n_sold in history
        if n and (n_sold / n) >= threshold
    }
    alerts = []
    if usually:
        rows = conn.execute(
            f"""
            WITH final AS ({_final_sql('target_date = ?')})
            SELECT f.shop_id, s.name AS shop_name, f.cast_name,
                   f.status_norm, f.status_raw,
                   CAST(COALESCE(f.cast_id, f.cast_name) AS TEXT) AS ckey
            FROM final f JOIN shops s ON s.shop_id = f.shop_id
            WHERE f.status_norm IN ('available', 'low')""",
            (target_date,),
        ).fetchall()
        for shop_id, shop_name, cast, norm, raw, ckey in rows:
            if (shop_id, ckey) in usually:
                n, n_sold = usually[(shop_id, ckey)]
                alerts.append((shop_name, cast, norm, raw, n_sold, n,
                               round(n_sold / n, 2)))
    alerts.sort()
    return alerts, considered


def alert_evidence_lines(conn, shop_name, cast_name, date, prefix="      ",
                         review_db=None, bakusai_db=None, compact=False):
    """Best-effort evidence lines for one alert.

    Display only: a broken/missing review or bakusai DB must never
    suppress or alter the alert itself, hence the blanket except."""
    try:
        row = conn.execute(
            "SELECT shop_id FROM shops WHERE name = ?",
            (shop_name,)).fetchone()
        if not row:
            return []
        ev = evidence_mod.collect(
            conn, row[0], shop_name, cast_name, date,
            review_db=review_db, bakusai_db=bakusai_db)
        return evidence_mod.format_lines(ev, prefix=prefix, compact=compact)
    except Exception:
        return []


def rollup_daily(conn, date=None):
    """Materialize the R3 effective final state into daily_cast_status.

    One row per (shop_id, cast_key, target_date); cast_key is
    CAST(COALESCE(cast_id, cast_name) AS TEXT), the same key every
    aggregation uses. Idempotent rebuild per day (delete + insert), so
    it is safe to call after every import - the runner does exactly
    that, making alert history, sold-out streaks and trends a plain
    table lookup instead of a window pass. Returns rows written.
    """
    # first_sold_out_at / was_low landed after the table shipped;
    # upgrade old DBs in place
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(daily_cast_status)")}
    if "first_sold_out_at" not in cols:
        conn.execute(
            "ALTER TABLE daily_cast_status"
            " ADD COLUMN first_sold_out_at TEXT")
    if "was_low" not in cols:
        conn.execute(
            "ALTER TABLE daily_cast_status"
            " ADD COLUMN was_low INTEGER NOT NULL DEFAULT 0")

    if date:
        days = [date]
    else:
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT target_date FROM snapshots")]
    total = 0
    for d in days:
        rows = conn.execute(_final_sql("target_date = ?"), (d,)).fetchall()
        first_so = {f"{s}|{k}": fa for s, k, fa in conn.execute(
            """SELECT shop_id, CAST(COALESCE(cast_id, cast_name) AS TEXT),
                      MIN(fetched_at)
               FROM snapshots
               WHERE target_date = ? AND status_norm = 'sold_out'
               GROUP BY 1, 2""", (d,)).fetchall()}
        # was_low: 'low' seen before the first sold_out, or at all on a
        # day the cast never sold out. Both cases count as a forecast
        # candidate for forecast_eval self-scoring.
        low_seen = {}
        for s, k, fa in conn.execute(
                """SELECT shop_id,
                          CAST(COALESCE(cast_id, cast_name) AS TEXT),
                          MIN(fetched_at)
                   FROM snapshots
                   WHERE target_date = ? AND status_norm = 'low'
                   GROUP BY 1, 2""", (d,)).fetchall():
            so_at = first_so.get(f"{s}|{k}")
            low_seen[f"{s}|{k}"] = so_at is None or fa < so_at
        conn.execute(
            "DELETE FROM daily_cast_status WHERE target_date = ?", (d,))
        conn.executemany(
            """INSERT INTO daily_cast_status
               (shop_id, cast_key, cast_id, cast_name, target_date,
                status_norm, status_raw, work_hours, age, fetched_at,
                first_sold_out_at, was_low)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(r[0], str(r[7] if r[7] is not None else r[1]), r[7], r[1],
              r[2], r[3], r[4], r[6], r[9], r[8],
              first_so.get(f"{r[0]}|{r[7] if r[7] is not None else r[1]}"),
              1 if low_seen.get(
                  f"{r[0]}|{r[7] if r[7] is not None else r[1]}") else 0)
             for r in rows],
        )
        # forecast self-score: of the day's was_low casts, how many sold
        # out (any prior-day history requirement stays in the report;
        # here we score the raw signal so the trend is comparable)
        cand = sum(1 for r in rows if low_seen.get(
            f"{r[0]}|{r[7] if r[7] is not None else r[1]}"))
        hits = sum(1 for r in rows
                   if r[3] == "sold_out" and low_seen.get(
                       f"{r[0]}|{r[7] if r[7] is not None else r[1]}"))
        conn.execute(
            """INSERT INTO forecast_eval
               (target_date, candidates, hits, evaluated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(target_date) DO UPDATE SET
                 candidates = excluded.candidates,
                 hits = excluded.hits,
                 evaluated_at = excluded.evaluated_at""",
            (d, cand, hits,
             now_jst().strftime("%Y-%m-%d %H:%M:%S") + " JST"))
        total += len(rows)
    conn.commit()
    return total
