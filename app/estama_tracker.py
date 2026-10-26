#!/usr/bin/env python3
"""estama-tracker: track sold-out casts on Estama (estama.jp), Osaka only.

Python standard library only. No live scraping, no login, no reservation.

Design (see README.md):
- The watchlist is configured in shops.json, NOT derived from live list
  order on each run. Scope change 2026-10-26: the final watchlist will be
  the 20 Osaka shops with the most reviews. Until shops.json is filled,
  there is no final watchlist.
- Snapshots are imported from JSON files in a fixed format
  (sample_2026-10-26_top20.json is an example of that format, kept as a
  SAMPLE only). A future review-rich snapshot is dropped in with the
  same format and loaded with `load` -- no code change needed.
- Sold-out is decided ONLY from the therapist-level status text
  (status_raw -> status_norm). Slot symbols (○/×/─) are auxiliary text
  and are NEVER used to decide sold_out.

Usage:
    python estama_tracker.py status
    python estama_tracker.py load <snapshot.json> [--shops shops.json] [--db estama.db]
    python estama_tracker.py sample [--db estama_sample.db]
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

BASE = Path(__file__).resolve().parent
SCHEMA = BASE / "schema.sql"
DEFAULT_SHOPS = BASE / "shops.json"
SAMPLE = BASE / "sample_2026-10-26_top20.json"


def normalize_status(raw):
    """Map a therapist-level status string to a normalized status.

    Order matters: sold_out is checked before 'closed', because the
    wording for sold-out contains neither '受付終了' nor '残りわずか'.
    '受付終了' / '本日受付終了' is 'closed', explicitly NOT sold_out.
    Anything unrecognised (including '時間のみ' and empty) is 'unknown'.
    Slot symbols are never passed to this function.
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


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def load_watchlist(shops_path):
    """Return (watchlist_dict, set_of_shop_ids_or_None).

    None means 'no filtering': the shops file was absent or its shop
    list is empty. Callers must treat an empty watchlist as 'pending',
    not as 'use the sample'.
    """
    if shops_path is None or not Path(shops_path).exists():
        return {}, None
    data = json.loads(Path(shops_path).read_text(encoding="utf-8"))
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
    return conn.execute(
        """SELECT s.name, sn.cast_name, sn.shift, sn.status_raw
           FROM snapshots sn JOIN shops s ON s.shop_id = sn.shop_id
           WHERE sn.target_date = ? AND sn.status_norm = 'sold_out'
           ORDER BY s.name, sn.cast_name""",
        (date,),
    ).fetchall()


def status_counts(conn, date):
    return conn.execute(
        """SELECT status_norm, COUNT(*) FROM snapshots
           WHERE target_date = ? GROUP BY status_norm ORDER BY status_norm""",
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
    a day is the status of that day's LATEST observation (by
    fetched_time, then snapshot_id), so intraday checkpoints can no
    longer inflate the history (the pre-2026-10-26 code counted rows
    and fired on 3 checkpoints of a single day). A cast qualifies
    when it was observed on >= min_days distinct days before the
    target date, its sold_out rate over those DAYS is >= threshold,
    and its target-date day status is 'available' or 'low'.
    Returns (alerts, history_casts_considered).
    """
    history = conn.execute(
        """WITH daily AS (
             SELECT shop_id, cast_name, target_date, status_norm,
                    ROW_NUMBER() OVER (
                        PARTITION BY shop_id, cast_name, target_date
                        ORDER BY fetched_time DESC, snapshot_id DESC
                    ) AS rn
             FROM snapshots WHERE target_date < ?
           )
           SELECT shop_id, cast_name,
                  COUNT(*) AS n_days,
                  SUM(CASE WHEN status_norm = 'sold_out' THEN 1 ELSE 0 END) AS n_sold_days
           FROM daily WHERE rn = 1
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
            """WITH daily AS (
                 SELECT sn.shop_id, s.name AS shop_name, sn.cast_name,
                        sn.status_norm, sn.status_raw,
                        ROW_NUMBER() OVER (
                            PARTITION BY sn.shop_id, sn.cast_name, sn.target_date
                            ORDER BY sn.fetched_time DESC, sn.snapshot_id DESC
                        ) AS rn
                 FROM snapshots sn JOIN shops s ON s.shop_id = sn.shop_id
                 WHERE sn.target_date = ?
               )
               SELECT shop_id, shop_name, cast_name, status_norm, status_raw
               FROM daily WHERE rn = 1 AND status_norm IN ('available', 'low')""",
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
    p = sub.add_parser("sample", help="load the top-20 SAMPLE file only")
    p.add_argument("--db", default=str(BASE / "estama_sample.db"))
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
            print("watchlist: PENDING - shops.json has no shops yet.")
            print("Final watchlist = 20 Osaka shops with the most reviews, "
                  "to be collected separately.")
            print("Sample file available: sample_2026-10-26_top20.json "
                  "(SAMPLE ONLY, not the final watchlist).")
        else:
            print(f"watchlist: {data.get('watchlist_name')} - "
                  f"{len(ids)} shops configured")
        return 0

    if args.cmd == "sample":
        conn = connect(args.db)
        stats = load_snapshot(conn, SAMPLE, watchlist_ids=None)
        print(f"sample loaded into {args.db} (SAMPLE ONLY, not final): {stats}")
        print_verify(conn)
        return 0

    if args.cmd == "load":
        _, ids = load_watchlist(args.shops)
        if ids is None:
            print("WARNING: shops.json watchlist is empty/pending. "
                  "Loading without a watchlist filter; this load is NOT "
                  "a finalized watchlist. Fill shops.json with the "
                  "review-rich 20 before treating a DB as final.")
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
