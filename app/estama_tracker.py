#!/usr/bin/env python3
"""estama-tracker: track sold-out casts on Estama (estama.jp), Osaka only.

Python standard library only. No live scraping, no login, no reservation.

Design (see README.md):
- The watchlist lives in shops.json and is maintained by the ranking
  scraper's sync (plus the runner's ban list), NOT derived from live
  list order on each run.
- In production the snapshots table is written by estama_scraper.py;
  this tracker is the analysis side (sold-out lists, counts, alerts).
  `load` still imports snapshot JSON files in the original format.
- Sold-out is decided ONLY from the therapist-level status text
  (status_raw -> status_norm). Slot symbols (○/×/─) are auxiliary text
  and are NEVER used to decide sold_out.

Usage:
    python estama_tracker.py status
    python estama_tracker.py load <snapshot.json> [--shops shops.json] [--db estama.db]
    python estama_tracker.py sold-out [--date YYYY-MM-DD] [--db DB]
    python estama_tracker.py counts [--date YYYY-MM-DD] [--db DB]
    python estama_tracker.py alert --date YYYY-MM-DD [--min-days 3] [--threshold 0.7] [--db DB]
    python estama_tracker.py demo
    python estama_tracker.py verify [--db DB]
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from common import (BASE_DIR as BASE, SCHEMA_PATH as SCHEMA,
                    SHOPS_JSON_PATH as DEFAULT_SHOPS, connect_db, load_json,
                    normalize_status_text)


def normalize_status(raw):
    """Kept for compatibility; the mapping lives in common.py."""
    return normalize_status_text(raw)


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
_FINAL_PARTITION = "shop_id, cast_name, target_date"


def _final_window(partition: str = _FINAL_PARTITION) -> str:
    """Return the R3 ranking window for one cast-day."""
    return f"""(
      PARTITION BY {partition}
      ORDER BY (status_norm = 'closed'),
               COALESCE(fetched_at, fetched_time, '') DESC,
               snapshot_id DESC
    )"""


def _final_sql(where: str) -> str:
    """SELECT of R3 effective final rows; rn_eff=1 per partition."""
    return f"""
        WITH ranked AS (
          SELECT shop_id, cast_name, target_date, status_norm, status_raw,
                 shift, work_hours, fetched_at, fetched_time, snapshot_id,
                 ROW_NUMBER() OVER {_final_window()} AS rn_eff
          FROM snapshots
          WHERE {where}
        )
        SELECT shop_id, cast_name, target_date, status_norm, status_raw,
               shift, work_hours
        FROM ranked WHERE rn_eff = 1
    """


def final_rows(conn: sqlite3.Connection, date: str):
    """R3 effective final state per cast for target_date (analysis-time).

    Returns (shop_id, cast_name, target_date, status_norm, status_raw,
    shift, work_hours) - one row per cast. Never writes to the DB.
    """
    return conn.execute(
        _final_sql("target_date = ?"),
        (date,),
    ).fetchall()


def connect(db_path):
    return connect_db(db_path)


def load_watchlist(shops_path):
    """Return (watchlist_dict, set_of_shop_ids_or_None).

    None means 'no filtering': the shops file was absent or its shop
    list is empty. Callers must treat an empty watchlist as 'pending',
    not as 'use the sample'.
    """
    if shops_path is None or not Path(shops_path).exists():
        return {}, None
    data = load_json(shops_path, default={})
    ids = {int(s["shop_id"]) for s in data.get("shops", [])}
    return data, (ids if ids else None)


def load_snapshot(conn, snapshot_path, watchlist_ids=None):
    """Insert a snapshot JSON file. Idempotent for same date/shop/cast.

    Returns a dict of load statistics. Shops not in the watchlist (when
    a non-empty watchlist is given) are skipped and counted.
    """
    data = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    date = data["snapshot_date"]
    fetched = data.get("collected_jst", "")
    stats = {
        "date": date,
        "shops_seen": 0,
        "shops_inserted": 0,
        "shops_skipped_not_in_watchlist": 0,
        "casts_inserted": 0,
        "casts_skipped_not_in_watchlist": 0,
        "shops_with_no_table": 0,
    }
    for shop in data["shops"]:
        stats["shops_seen"] += 1
        shop_id = int(shop["shop_id"])
        if watchlist_ids is not None and shop_id not in watchlist_ids:
            stats["shops_skipped_not_in_watchlist"] += 1
            stats["casts_skipped_not_in_watchlist"] += len(shop.get("casts", []))
            continue
        conn.execute(
            """INSERT INTO shops (shop_id, name, area, store_status, source_url)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (shop_id) DO UPDATE SET
                 name = excluded.name, area = excluded.area,
                 store_status = excluded.store_status,
                 source_url = excluded.source_url""",
            (shop_id, shop["name"], shop.get("area"),
             shop.get("store_status"), shop.get("source_url")),
        )
        stats["shops_inserted"] += 1
        if not shop.get("has_table", True):
            stats["shops_with_no_table"] += 1
        for cast in shop.get("casts", []):
            raw = cast.get("status_raw")
            cur = conn.execute(
                """INSERT OR IGNORE INTO snapshots
                   (shop_id, cast_name, cast_id, age, shift, status_raw,
                    status_norm, slot_pattern, target_date, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (shop_id, cast["cast_name"], cast.get("cast_id"),
                 cast.get("age"), cast.get("shift"), raw,
                 normalize_status(raw), cast.get("slot_pattern"),
                 date, fetched),
            )
            # rowcount is 1 only when a new row was actually inserted;
            # 0 on a re-run for the same date/shop/cast (idempotent).
            if cur.rowcount:
                stats["casts_inserted"] += 1
    conn.commit()
    return stats


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
        SELECT shop_id, cast_name,
               COUNT(*) AS n_days,
               SUM(CASE WHEN status_norm = 'sold_out' THEN 1 ELSE 0 END) AS n_sold_days
        FROM final
        GROUP BY shop_id, cast_name HAVING COUNT(*) >= ?""",
        (target_date, min_days),
    ).fetchall()
    considered = len(history)
    usually = {
        (shop_id, cast): (n, n_sold)
        for shop_id, cast, n, n_sold in history
        if n and (n_sold / n) >= threshold
    }
    alerts = []
    if usually:
        rows = conn.execute(
            f"""
            WITH final AS ({_final_sql('target_date = ?')})
            SELECT f.shop_id, s.name AS shop_name, f.cast_name,
                   f.status_norm, f.status_raw
            FROM final f JOIN shops s ON s.shop_id = f.shop_id
            WHERE f.status_norm IN ('available', 'low')""",
            (target_date,),
        ).fetchall()
        for shop_id, shop_name, cast, norm, raw in rows:
            if (shop_id, cast) in usually:
                n, n_sold = usually[(shop_id, cast)]
                alerts.append((shop_name, cast, norm, raw, n_sold, n,
                               round(n_sold / n, 2)))
    alerts.sort()
    return alerts, considered


def run_demo():
    """In-memory proof that the alert logic triggers. NOT saved to disk.

    Synthetic history only: cast 'デモA' is sold out on 3 prior days
    and available on the target day (must alert); 'デモB' is sold out
    1/3 prior days (must not); 'デモC' is sold out 3/3 but still sold
    out on the target day (must not); 'デモD' was never sold out
    (must not).
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO shops VALUES (1, 'デモ店', '梅田', NULL, NULL)")

    def add(cast, date, norm, raw):
        conn.execute(
            """INSERT INTO snapshots
               (shop_id, cast_name, status_raw, status_norm, target_date)
               VALUES (1, ?, ?, ?, ?)""",
            (cast, raw, norm, date),
        )

    for d in ("2026-10-23", "2026-10-24", "2026-10-25"):
        add("デモA", d, "sold_out", "ご好評につき本日完売")
        add("デモC", d, "sold_out", "ご好評につき本日完売")
    add("デモB", "2026-10-23", "sold_out", "ご好評につき本日完売")
    add("デモB", "2026-10-24", "available", "ご予約受付中")
    add("デモB", "2026-10-25", "available", "ご予約受付中")
    add("デモD", "2026-10-23", "available", "ご予約受付中")
    add("デモD", "2026-10-24", "available", "ご予約受付中")
    add("デモD", "2026-10-25", "available", "ご予約受付中")
    add("デモA", "2026-10-26", "available", "ご予約受付中")
    add("デモC", "2026-10-26", "sold_out", "ご好評につき本日完売")
    conn.commit()
    alerts, considered = usually_sold_out_alert(conn, "2026-10-26")
    assert considered == 4, considered
    assert [a[1] for a in alerts] == ["デモA"], alerts
    print("demo: alert logic OK - only デモA triggered "
          "(3/3 prior days sold out, available on target day)")
    print("demo: デモB (1/3), デモC (still sold out), デモD (never) "
          "correctly did not trigger")
    return True


def print_verify(conn):
    shops = conn.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
    rows = conn.execute(
        "SELECT target_date, COUNT(*) FROM snapshots GROUP BY target_date"
    ).fetchall()
    print(f"shops in DB: {shops}")
    for date, n in rows:
        sold = conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE target_date = ? "
            "AND status_norm = 'sold_out'",
            (date,),
        ).fetchone()[0]
        no_table = shops_with_no_table(conn, date)
        print(f"  {date}: cast rows={n}, sold_out={sold}, "
              f"shops with no rows that date={len(no_table)}")
    if not rows:
        print("  (no snapshot rows loaded yet)")


def main(argv=None):
    parser = argparse.ArgumentParser(description="estama-tracker")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="show watchlist status")
    p = sub.add_parser("load", help="load a snapshot JSON file")
    p.add_argument("snapshot")
    p.add_argument("--shops", default=str(DEFAULT_SHOPS))
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("sold-out", help="today's sold-out list")
    p.add_argument("--date", required=True)
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("counts", help="status counts for a date")
    p.add_argument("--date", required=True)
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("alert", help="usually-sold-out but available/low")
    p.add_argument("--date", required=True)
    p.add_argument("--min-days", "--min-snapshots", dest="min_days",
                   type=int, default=3,
                   help="minimum distinct history DAYS (was: snapshot rows)")
    p.add_argument("--threshold", type=float, default=0.7)
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("demo", help="in-memory alert-logic test")
    p = sub.add_parser("verify", help="print DB counts")
    p.add_argument("--db", default=str(BASE / "estama.db"))

    args = parser.parse_args(argv)

    if args.cmd == "demo":
        return 0 if run_demo() else 1

    if args.cmd == "status":
        data, ids = load_watchlist(DEFAULT_SHOPS)
        if ids is None:
            print("watchlist: shops.json is missing or empty.")
        else:
            print(f"watchlist: {data.get('watchlist_name')} - "
                  f"{len(ids)} shops configured")
        return 0

    if args.cmd == "load":
        _, ids = load_watchlist(args.shops)
        if ids is None:
            print("WARNING: shops.json watchlist is empty. "
                  "Loading without a watchlist filter.")
        conn = connect(args.db)
        stats = load_snapshot(conn, args.snapshot, watchlist_ids=ids)
        print(f"loaded into {args.db}: {stats}")
        print_verify(conn)
        return 0

    conn = connect(args.db)
    if args.cmd == "sold-out":
        rows = sold_out(conn, args.date)
        if not rows:
            print(f"{args.date}: no sold-out casts in DB")
        for shop, cast, shift, raw in rows:
            print(f"{shop} - {cast} (shift: {shift}) [{raw}]")
        return 0
    if args.cmd == "counts":
        for norm, n in status_counts(conn, args.date):
            print(f"{norm}: {n}")
        return 0
    if args.cmd == "alert":
        alerts, considered = usually_sold_out_alert(
            conn, args.date, args.min_days, args.threshold)
        if considered == 0 or not alerts:
            if considered == 0:
                print("alert: history insufficient - no cast has enough "
                      "prior snapshots to judge 'usually sold out'. "
                      "Add more daily snapshots first; nothing is faked.")
            else:
                print(f"alert: no usually-sold-out casts available/low on "
                      f"{args.date} ({considered} casts with enough history)")
        for shop, cast, norm, raw, n_sold, n, rate in alerts:
            print(f"ALERT {shop} - {cast}: {norm} [{raw}] "
                  f"(sold out {n_sold}/{n} prior days, rate {rate})")
        return 0
    if args.cmd == "verify":
        print_verify(conn)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
