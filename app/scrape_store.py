"""DB-writing side of the estama_scraper pipeline.


Kept out of estama_scraper.py so the DB code is not buried in CLI
plumbing. Imports the pure parsers from scrape_parse.py.
"""
import sqlite3

from common import (CHRONO_KEY_SQL, DB_PATH as DEFAULT_DB, business_date,
                    connect_db, DEFAULT_SHOP_ID, now_jst, parse_clock)
from common import (COLOR_RED, COLOR_GREEN, COLOR_YELLOW, COLOR_CYAN,
                    COLOR_MAGENTA, COLOR_BOLD, COLOR_DIM, COLOR_RESET)
from scrape_parse import _slot_within_shift, normalize_slot_mark

def record_slot_events(conn, shop_id, cast, target_date, fetched_at,
                       fetched_time):
    """Append 30-min state transitions for one cast to slot_events.

    Only differences are stored (the first sighting has from_mark
    NULL), so re-importing the same checkpoint inserts nothing.
    Off-shift '─' cells are the site's filler, not a state change, and
    are skipped. Returns the number of rows inserted.
    """
    slots = cast.get("slots") or []
    if not slots:
        return 0
    # A newer checkpoint already saw this cast-day, so a late replay of
    # an older snapshot must not rewrite the recorded state.
    newer = conn.execute(
        "SELECT 1 FROM snapshots WHERE shop_id = ? AND target_date = ?"
        " AND fetched_at > ? LIMIT 1",
        (shop_id, target_date, fetched_at)).fetchone()
    if newer:
        return 0
    cast_id = cast.get("cast_id")
    cast_key = str(cast_id) if cast_id is not None else cast["cast_name"]
    inserted = 0
    for slot_time, raw_mark in slots:
        mark = normalize_slot_mark(raw_mark)
        if mark is None:
            continue
        minutes = parse_clock(slot_time)
        if minutes is None:
            continue
        prior = conn.execute(
            """SELECT to_mark, observed_at FROM slot_events
               WHERE shop_id = ? AND cast_key = ? AND target_date = ?
                 AND slot_time = ?
               ORDER BY observed_at DESC, event_id DESC LIMIT 1""",
            (shop_id, cast_key, target_date, slot_time)).fetchone()
        if mark == "─":
            inside = _slot_within_shift(
                minutes, cast.get("shift_start"), cast.get("shift_end"))
            if inside is False:
                continue
            if inside is None and prior is None:
                continue  # unknown shift: don't seed filler dashes
        if prior is not None:
            prior_mark, prior_obs = prior
            if prior_obs > fetched_at:
                continue  # late replay of an older checkpoint
            if prior_mark == mark:
                continue
        conn.execute(
            """INSERT OR IGNORE INTO slot_events (
                   shop_id, cast_key, cast_id, cast_name, target_date,
                   slot_time, slot_minutes, from_mark, to_mark,
                   observed_at, checkpoint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (shop_id, cast_key, cast_id, cast["cast_name"], target_date,
             slot_time, minutes, prior[0] if prior else None, mark,
             fetched_at, fetched_time))
        inserted += 1
    return inserted



def import_into_db(data, db_path=DEFAULT_DB):
    """Import multi-day scrape result directly into SQLite estama.db with fetched_time and work hours."""
    conn = connect_db(db_path)

    shop = data["shop"]
    conn.execute(
        """
        INSERT OR REPLACE INTO shops (shop_id, name, area, store_status, source_url)
        VALUES (?, ?, ?, ?, ?)
        """,
        (shop["shop_id"], shop["name"], shop["area"], shop["store_status"], shop["source_url"])
    )

    total_inserted = 0
    fetched_time = data.get("fetched_time", now_jst().strftime("%H:%M"))
    for day_info in data["days"]:
        target_date = day_info["target_date"]
        for c in day_info["casts"]:
            conn.execute(
                """
                INSERT OR REPLACE INTO snapshots (
                    shop_id, cast_name, cast_id, age, shift,
                    shift_start, shift_end, work_minutes, work_hours,
                    status_raw, status_norm, slot_pattern, target_date, fetched_at, fetched_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shop["shop_id"],
                    c["cast_name"],
                    c["cast_id"],
                    c["age"],
                    c["shift"],
                    c.get("shift_start"),
                    c.get("shift_end"),
                    c.get("work_minutes"),
                    c.get("work_hours"),
                    c["status_raw"],
                    c["status_norm"],
                    c["slot_pattern"],
                    target_date,
                    data["fetched_at"],
                    fetched_time
                )
            )
            total_inserted += 1
            # Future days only carry templated slot grids, so their
            # "changes" are noise; slot history is today-only.
            if day_info.get("days_offset") == 0:
                record_slot_events(conn, shop["shop_id"], c, target_date,
                                   data["fetched_at"], fetched_time)

    conn.commit()
    conn.close()
    return total_inserted


def _speed_rank_badge(first_time):
    """Return the sold-out speed badge for a first observed time."""
    if not first_time:
        return f"{COLOR_DIM}【判定不可】{COLOR_RESET}"
    mins = parse_clock(first_time)
    if mins is None:
        return f"{COLOR_DIM}【判定不可】{COLOR_RESET}"
    if mins < parse_clock("06:00"):
        return f"{COLOR_MAGENTA}【深夜完売 🌙】{COLOR_RESET}"
    if mins <= parse_clock("12:00"):
        return f"{COLOR_RED}【超速完売 ⚡ プレミア】{COLOR_RESET}"
    if mins <= parse_clock("17:00"):
        return f"{COLOR_YELLOW}【夕方完売 🔥 人気】{COLOR_RESET}"
    return f"{COLOR_CYAN}【夜間完売】{COLOR_RESET}"


def show_speed_ranking(date_str=None, db_path=DEFAULT_DB, shop_id=DEFAULT_SHOP_ID):
    """Analyze and display which casts sold out earliest on a given date."""
    if date_str is None:
        date_str = business_date()

    conn = connect_db(db_path, apply_schema=False)
    cur = conn.cursor()

    # Query earliest sold-out time for each cast on target date.
    # `k` is a row-level sort key: full fetched_at when present (orders
    # 00-05 next-calendar-day checkpoints of the same target_date
    # correctly), an HH:MM-only fallback for legacy rows (day unknown,
    # rank behind full-timestamp rows). The old MIN(fetched_time)
    # comparison reported a 13:33 sold-out as "00:33" (21 real cases).
    # Grouped by cast_id (TEXT-cast, name fallback): a renamed cast
    # (one id, two names) appears once; name / shift / work_hours all
    # come from the latest sold-out observation so they stay consistent.
    query = f"""
    WITH so AS (
      SELECT CAST(COALESCE(cast_id, cast_name) AS TEXT) AS g,
             {CHRONO_KEY_SQL} AS k,
             cast_name, shift, work_hours, snapshot_id
      FROM snapshots
      WHERE target_date = ? AND shop_id = ? AND status_norm = 'sold_out'
    )
    SELECT
        (SELECT s2.cast_name FROM so s2 WHERE s2.g = so.g
          ORDER BY s2.k DESC, s2.snapshot_id DESC LIMIT 1) AS cast_name,
        substr(MIN(so.k), 12, 5) AS first_sold_out_time,
        COUNT(*) AS total_records,
        (SELECT s2.shift FROM so s2 WHERE s2.g = so.g
          ORDER BY s2.k DESC, s2.snapshot_id DESC LIMIT 1) AS shift,
        (SELECT s2.work_hours FROM so s2 WHERE s2.g = so.g
          ORDER BY s2.k DESC, s2.snapshot_id DESC LIMIT 1) AS work_hours
    FROM so
    GROUP BY so.g
    ORDER BY MIN(so.k), work_hours DESC
    """
    cur.execute(query, (date_str, shop_id))
    sold_out_rows = cur.fetchall()

    # Query all active checkpoints on that date, in real chronological
    # order (fetched_at across midnight, not fetched_time string order)
    # Checkpoint column order = row-level time key (same rule as the
    # rank query: full fetched_at, legacy HH:MM ranked behind)
    cur.execute(
        "SELECT fetched_time FROM snapshots WHERE target_date = ? AND shop_id = ? "
        "GROUP BY fetched_time "
        "ORDER BY MIN(" + CHRONO_KEY_SQL + ")",
        (date_str, shop_id)
    )
    recorded_times = [r[0] for r in cur.fetchall()]

    print("\n" + "=" * 75)
    print(f"{COLOR_BOLD}⚡ 完売スピード分析ランキング (日付: {date_str} / 店舗ID: {shop_id}){COLOR_RESET}")
    print(f"📊 記録済みチェックポイント ({len(recorded_times)}回): {', '.join(recorded_times) if recorded_times else 'なし'}")
    print("=" * 75)

    if not sold_out_rows:
        print(f"  {COLOR_DIM}{date_str} に完売検知されたキャストはまだありません。{COLOR_RESET}")
    else:
        medals = ["🥇", "🥈", "🥉"]
        for idx, (name, first_time, rec_cnt, shift, work_hours) in enumerate(sold_out_rows, 1):
            rank_str = medals[idx - 1] if idx <= 3 else f"{idx:2d}位"
            speed_badge = _speed_rank_badge(first_time)
            if shift and work_hours:
                hours_tag = f"{COLOR_MAGENTA}[ロング {work_hours}h]{COLOR_RESET}" if work_hours >= 8.0 else f"[{work_hours}h]"
                shift_info = f"{hours_tag} {shift}"
            elif shift:
                shift_info = f"[{shift}]"
            else:
                shift_info = "[シフト時間未定]"

            print(f"  {rank_str} {COLOR_BOLD}{name:<12}{COLOR_RESET} {speed_badge:<26} 完売検知: {COLOR_BOLD}{first_time}{COLOR_RESET}  {shift_info}")

    # Show timeline matrix if multiple times are recorded
    if len(recorded_times) > 1:
        print("\n" + "-" * 75)
        print(f"{COLOR_BOLD}📈 時系列タイムライン（各チェックポイントでの推移）{COLOR_RESET}")
        print("-" * 75)
        # Fetch status matrix keyed by cast id (TEXT-cast, name
        # fallback); rendered in cast-name order, name = latest
        # observation (renames don't split one cast into two rows).
        # Rows without a checkpoint time are skipped (none in prod;
        # keeps recorded_times formatting crash-free).
        cur.execute(
            """
            SELECT CAST(COALESCE(cast_id, cast_name) AS TEXT) AS ck,
                   cast_name, fetched_time, status_norm
            FROM snapshots
            WHERE target_date = ? AND shop_id = ?
            ORDER BY ck, fetched_at, snapshot_id
            """,
            (date_str, shop_id)
        )
        matrix = {}
        mnames = {}
        for ck, cname, ftime, norm in cur.fetchall():
            if ftime is None or ftime == "":
                continue
            mnames[ck] = cname  # last row wins = newest name
            matrix.setdefault(ck, {})[ftime] = norm

        header = f"  {'キャスト名':<12} " + " ".join([f"{t:>6}" for t in recorded_times])
        print(COLOR_DIM + header + COLOR_RESET)

        for ck in sorted(matrix, key=lambda k: mnames[k]):
            cname = mnames[ck]
            tdict = matrix[ck]
            row_items = []
            for t in recorded_times:
                st = tdict.get(t, "-")
                if st == "sold_out":
                    row_items.append(f"{COLOR_RED}完売{COLOR_RESET}")
                elif st in ("available", "low"):
                    row_items.append(f"{COLOR_GREEN}空き{COLOR_RESET}")
                elif st == "closed":
                    row_items.append(f"{COLOR_DIM}終了{COLOR_RESET}")
                else:
                    row_items.append(f"{COLOR_CYAN}不明{COLOR_RESET}")
            print(f"  {cname:<12} " + "   ".join(row_items))

    print("=" * 75 + "\n")
    conn.close()


