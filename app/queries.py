"""SQL/query helpers shared by the tracker, analysis and reports.

The R3 effective-final-state window lives here once, together with the
lookups every report section used to rewrite (shop names/areas, cast
keys, observed dates). Pure queries: no printing, no formatting.
"""
import sqlite3


# cast_key: one cast must stay one person even when renamed
# (cast_id wins; legacy rows without cast_id fall back to the name).
CAST_KEY_SQL = "CAST(COALESCE(cast_id, cast_name) AS TEXT)"

# R3 effective final state, analysis-time only (DB rows stay raw).
#
# The site resets every cast to 'closed' at end of shift / at the
# shop-wide closing flip (e.g. FROGSPA 01:33-05:31 all closed). Counting
# that last row would report almost everyone 'closed'. Rule R3: if the
# latest observation of a cast-day is 'closed', walk back to the LATEST
# non-closed observation of that day; if the whole day is closed, the
# effective state stays 'closed'.
#
# One window pass does both steps: sort closed rows last (0/1 key), then
# newest first - rn_eff=1 is the last non-closed row if one exists,
# otherwise the newest row (closed). Chronology uses the full fetched_at
# timestamp ('YYYY-MM-DD HH:MM:SS JST' - every row has it; lexicographic
# order == chronological across midnight). fetched_time alone would be
# wrong across midnight (05:34 < 23:37) and snapshot_id is the tiebreak
# for rows with no timestamps (demo inserts).
_FINAL_PARTITION = "shop_id, CAST(COALESCE(cast_id, cast_name) AS TEXT), target_date"





def _final_window(partition: str = _FINAL_PARTITION) -> str:
    """Return the R3 ranking window for one cast-day."""
    return f"""(
      PARTITION BY {partition}
      ORDER BY (status_norm = 'closed'),
               COALESCE(fetched_at, fetched_time, '') DESC,
               snapshot_id DESC
    )"""


def _final_sql(where: str) -> str:
    """SELECT of R3 effective final rows; rn_eff=1 per partition.

    cast_id is returned (appended) so callers can group/alert by the
    stable id: a renamed cast (one cast_id, two names) must not split
    into two people. Rows without cast_id group by name (COALESCE in
    _FINAL_PARTITION)."""
    return f"""
        WITH ranked AS (
          SELECT shop_id, cast_name, cast_id, target_date, status_norm,
                 status_raw, shift, work_hours, fetched_at, fetched_time,
                 snapshot_id, age,
                 ROW_NUMBER() OVER {_final_window()} AS rn_eff
          FROM snapshots
          WHERE {where}
        )
        SELECT shop_id, cast_name, target_date, status_norm, status_raw,
               shift, work_hours, cast_id, fetched_at, age
        FROM ranked WHERE rn_eff = 1
    """


def final_rows(conn: sqlite3.Connection, date: str):
    """R3 effective final state per cast for target_date (analysis-time).

    Returns (shop_id, cast_name, target_date, status_norm, status_raw,
    shift, work_hours, cast_id) - one row per cast (grouped by
    cast_id, name fallback). Never writes to the DB.
    """
    return conn.execute(
        _final_sql("target_date = ?"),
        (date,),
    ).fetchall()


def _all_dates(conn):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT target_date FROM snapshots ORDER BY target_date")]


def _day_finals(conn, date):
    """R3 final rows as dicts for one date."""
    cols = ("shop_id", "cast_name", "target_date", "status_norm",
            "status_raw", "shift", "work_hours", "cast_id",
            "fetched_at", "age")
    return [dict(zip(cols, r)) for r in conn.execute(
        _final_sql("target_date = ?"), (date,)).fetchall()]


def _ckey(row):
    return str(row["cast_id"] if row["cast_id"] is not None
               else row["cast_name"])


def _resolve_cast(conn, shop_id, cast_id=None, cast_name=None):
    """Resolve (cast_key, cast_name) for a watch target from snapshots.

    cast_id wins when both are given; name-only watches key on the name
    (renames won't follow, which matches COALESCE semantics)."""
    if cast_id is not None:
        row = conn.execute(
            """SELECT CAST(COALESCE(cast_id, cast_name) AS TEXT), cast_name
               FROM snapshots WHERE shop_id = ? AND cast_id = ?
               ORDER BY fetched_at DESC LIMIT 1""",
            (shop_id, cast_id)).fetchone()
        if row:
            return row[0], row[1]
        return str(cast_id), cast_name or str(cast_id)
    row = conn.execute(
        """SELECT CAST(COALESCE(cast_id, cast_name) AS TEXT), cast_name
           FROM snapshots WHERE shop_id = ? AND cast_name = ?
           ORDER BY fetched_at DESC LIMIT 1""",
        (shop_id, cast_name)).fetchone()
    if row:
        return row[0], row[1]
    return str(cast_name), cast_name


def sold_out(conn, date):
    """Casts whose R3 effective final state is sold_out (analysis-time)."""
    rows = conn.execute(
        f"""
        WITH final AS ({_final_sql('target_date = ?')})
        SELECT s.name, f.cast_name, f.shift, f.status_raw
        FROM final f JOIN shops s ON s.shop_id = f.shop_id
        WHERE f.status_norm = 'sold_out'
        ORDER BY s.name, f.cast_name""",
        (date,),
    ).fetchall()
    return rows


def status_counts(conn, date):
    """Counts of R3 effective final states (one vote per cast)."""
    return conn.execute(
        f"""
        WITH final AS ({_final_sql('target_date = ?')})
        SELECT status_norm, COUNT(*) FROM final
        GROUP BY status_norm ORDER BY status_norm""",
        (date,),
    ).fetchall()


def shops_with_no_table(conn, date):
    """Shops in the DB that have no snapshot rows for the given date."""
    return conn.execute(
        """SELECT shop_id, name FROM shops
           WHERE shop_id NOT IN
             (SELECT DISTINCT shop_id FROM snapshots WHERE target_date = ?)
           ORDER BY shop_id""",
        (date,),
    ).fetchall()




# public aliases: the move kept the original names so callers/tests
# could be split apart without touching every call site
all_dates = _all_dates
day_finals = _day_finals
ckey = _ckey
resolve_cast = _resolve_cast


def shop_names(conn):
    """{shop_id: name} - the lookup ten report sections used to rewrite."""
    return {sid: name for sid, name in
            conn.execute("SELECT shop_id, name FROM shops")}


def shop_areas(conn):
    """{shop_id: area-or-placeholder} for the area breakdown."""
    return {sid: (area or "(未設定)") for sid, area in
            conn.execute("SELECT shop_id, area FROM shops")}
