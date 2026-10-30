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

Stage-4 layout: R3/query helpers live in queries.py, alert/rollup
logic in analysis.py, all report sections + dashboard/notify in
report_sections.py. This module keeps the CLI, the snapshot loader and
the report renderer, and re-exports the analysis names so
`import estama_tracker as t` keeps working (tests and scripts).

Usage:
    python estama_tracker.py status
    python estama_tracker.py load <snapshot.json> [--shops shops.json] [--db estama.db]
    python estama_tracker.py sold-out [--date YYYY-MM-DD] [--db DB]
    python estama_tracker.py counts [--date YYYY-MM-DD] [--db DB]
    python estama_tracker.py alert --date YYYY-MM-DD [--min-days 3] [--threshold 0.7] [--db DB]
    python estama_tracker.py rollup [--date YYYY-MM-DD] [--db DB]
    python estama_tracker.py report [--date YYYY-MM-DD] [--db DB]
    python estama_tracker.py demo
    python estama_tracker.py verify [--db DB]
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import evidence as evidence_mod
from common import (BASE_DIR as BASE, SCHEMA_PATH as SCHEMA,
                    SHOPS_JSON_PATH as DEFAULT_SHOPS, business_date,
                    connect_db, load_json, normalize_status_text,
                    notify_webhook, now_jst)
from queries import (_all_dates, _ckey, _day_finals, _final_sql,
                     _resolve_cast, final_rows, shop_areas, shop_names,
                     shops_with_no_table, sold_out, status_counts)
from analysis import (alert_evidence_lines, rollup_daily,
                      usually_sold_out_alert)
from report_sections import (
    CHECKPOINTS_PER_DAY, DEEP_SHOPS_JSON, FLIP_HOUR, MONITOR_EMPTY_MIN,
    MONITOR_FAIL_MIN, MONITOR_FAIL_RATE, MONITOR_SLOT_MIN_RATE,
    SELLOUT_HORIZON_HOURS, _bd_cond, _bd_hour, _badge,
    _checkpoints_expected, _low_coverage_shops, _slot_counts,
    build_dashboard, build_notify_text, load_deep_shop_ids, monitor_report,
    pooled_sellout_hazard, section_area, section_cast_detail,
    section_consistency, section_coverage, section_daily, section_forecast,
    section_monitor, section_momentum, section_new_casts, section_ranking,
    section_rank_moves, section_runlog, section_sellout_forecast,
    section_shops, section_streaks, section_timing, section_transitions,
    section_watch, sellout_forecast,
)





def normalize_status(raw):
    """Kept for compatibility; the mapping lives in common.py."""
    return normalize_status_text(raw)


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
    """Insert a snapshot JSON file. Idempotent for the same checkpoint.

    Returns a dict of load statistics. Shops not in the watchlist (when
    a non-empty watchlist is given) are skipped and counted.
    """
    data = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    date = data["snapshot_date"]
    fetched = data.get("collected_jst", "")
    # fetched_time is part of the snapshots unique key; leaving it NULL
    # would make re-loads duplicate rows (SQLite treats NULLs as distinct).
    fetched_time = data.get("fetched_time") or (
        fetched[11:16] if len(fetched) >= 16 else "")
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
                    status_norm, slot_pattern, target_date, fetched_at,
                    fetched_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (shop_id, cast["cast_name"], cast.get("cast_id"),
                 cast.get("age"), cast.get("shift"), raw,
                 normalize_status(raw), cast.get("slot_pattern"),
                 date, fetched, fetched_time),
            )
            # rowcount is 1 only when a new row was actually inserted;
            # 0 on a re-run for the same date/shop/cast (idempotent).
            if cur.rowcount:
                stats["casts_inserted"] += 1
    conn.commit()
    return stats


def _expected_business_date(now):
    """Same rule as runner/scraper: the business date flips at ~06:00 JST."""
    return business_date(now)


def print_report(conn, date=None, min_days=2, threshold=0.6,
                 streak_min=2, top_n=15, evidence=True):
    dates = _all_dates(conn)
    if not dates:
        print("report: snapshots が空です")
        return
    date = date or dates[-1]

    def head(t):
        print("\n" + "=" * 70)
        print(t)
        print("=" * 70)

    head(f"【0】監視（{date}）")
    for line in section_monitor(conn, date):
        print(line)
    head("【1】日次サマリー（R3最終状態）")
    for line in section_daily(conn, dates):
        print(line)
    head(f"【2】完売ストリーク（{streak_min}日連続以上）")
    for line in section_streaks(conn, dates, streak_min):
        print(line)
    head(f"【3】アラート: 普段完売なのに{date}は空き"
         f"（min_days={min_days}, threshold={threshold}）")
    alerts, considered = usually_sold_out_alert(
        conn, date, min_days, threshold)
    if not alerts:
        print(f"  該当なし（履歴充分なcast: {considered}）")
    low_cov = _low_coverage_shops(conn, date)
    name2sid = {n: s for s, n in shop_names(conn).items()}
    for i, (shop, cast, norm, raw, n_sold, n, rate) in enumerate(alerts):
        gap = "  <<巡回欠損:この店の今日の取得が少ない>>" \
            if name2sid.get(shop) in low_cov else ""
        print(f"  ALERT {shop} - {cast}: {norm} [{raw}] "
              f"(完売 {n_sold}/{n}日, {rate}){gap}")
        if evidence and i < 5:
            for line in alert_evidence_lines(conn, shop, cast, date):
                print(line)
    if evidence and len(alerts) > 5:
        print(f"  (根拠は上位5件のみ表示; 他 {len(alerts) - 5}件省略)")
    head(f"【4】完売予兆（{date} 現在 low の注目cast）")
    for line in section_forecast(conn, date, min_days):
        print(line)
    head(f"【4b】予約の勢い（{date} 枠消化トップ）")
    for line in section_momentum(conn, date):
        print(line)
    head(f"【4c】4時間以内に完売する確率（{date}）")
    for line in section_sellout_forecast(conn, date, top=5):
        print(line)
    head("【5】完売の初観測タイミング")
    for line in section_timing(conn, dates):
        print(line)
    head("【6】キャスト出入り・状態遷移")
    for line in section_transitions(conn, dates):
        print(line)
    head(f"【7】エリア別 sold_out 率（{date}）")
    for line in section_area(conn, date, top_n):
        print(line)
    head(f"【8】店舗別 sold_out 率 上位（{date}, 3名以上の店）")
    for line in section_shops(conn, date, top_n):
        print(line)
    head(f"【9】週間ランキング照合（{date}）")
    for line in section_ranking(conn, date):
        print(line)
    head("【9b】ランキング変動（大阪全体, 週間）")
    for line in section_rank_moves(conn):
        print(line)
    head(f"【9c】新規cast・移籍（{date}）")
    for line in section_new_casts(conn, date):
        print(line)
    head("【10】cast整合性・データ品質")
    for line in section_consistency(conn):
        print(line)
    head("【11】データカバレッジ監査")
    for line in section_coverage(conn, dates):
        print(line)
    head(f"【12】ウォッチ中キャスト（{date}）")
    for line in section_watch(conn, date):
        print(line)
    head("【13】runner ジョブ履歴")
    for line in section_runlog(conn):
        print(line)


def watch_add(conn, shop_id, cast_id=None, cast_name=None, note=None):
    ck, cname = _resolve_cast(conn, shop_id, cast_id, cast_name)
    conn.execute(
        """INSERT INTO cast_watch (cast_key, shop_id, cast_name,
                                    added_at, note)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(shop_id, cast_key) DO UPDATE SET
             cast_name = excluded.cast_name, note = excluded.note""",
        (ck, shop_id, cname,
         now_jst().strftime("%Y-%m-%d %H:%M:%S") + " JST", note))
    conn.commit()
    return ck, cname


def watch_remove(conn, shop_id, cast_id=None, cast_name=None):
    ck, _ = _resolve_cast(conn, shop_id, cast_id, cast_name)
    cur = conn.execute(
        "DELETE FROM cast_watch WHERE shop_id = ? AND cast_key = ?",
        (shop_id, ck))
    conn.commit()
    return cur.rowcount


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


def _date_arg(value):
    """argparse type: business dates are strict YYYY-MM-DD strings.

    datetime.fromisoformat would also accept a datetime, which would
    never equal a target_date and report an empty (misleading) day."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r} (expected YYYY-MM-DD)")


def _build_parser():
    parser = argparse.ArgumentParser(description="estama-tracker")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="show watchlist status")
    p = sub.add_parser("load", help="load a snapshot JSON file")
    p.add_argument("snapshot")
    p.add_argument("--shops", default=str(DEFAULT_SHOPS))
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("sold-out", help="today's sold-out list")
    p.add_argument("--date", required=True, type=_date_arg)
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("counts", help="status counts for a date")
    p.add_argument("--date", required=True, type=_date_arg)
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("alert", help="usually-sold-out but available/low")
    p.add_argument("--date", required=True, type=_date_arg)
    p.add_argument("--min-days", "--min-snapshots", dest="min_days",
                   type=int, default=3,
                   help="minimum distinct history DAYS (was: snapshot rows)")
    p.add_argument("--threshold", type=float, default=0.7)
    p.add_argument("--no-evidence", action="store_true",
                   help="omit review/bakusai/slot evidence lines")
    p.add_argument("--review-db", default=None,
                   help="review DB (default: "
                        f"{evidence_mod.default_review_db()})")
    p.add_argument("--bakusai-db", default=None,
                   help="bakusai DB (default: "
                        f"{evidence_mod.default_bakusai_db()})")
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("sellout-forecast",
                       help="P(sold out within N hours) for watch/deep casts")
    p.add_argument("--date", type=_date_arg,
                   help="business date (default: today)")
    p.add_argument("--at", help="reference clock HH:MM (default: now)")
    p.add_argument("--horizon", type=int, default=SELLOUT_HORIZON_HOURS)
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--deep-shops", default=str(DEEP_SHOPS_JSON))
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("demo", help="in-memory alert-logic test")
    p = sub.add_parser("verify", help="print DB counts")
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("rollup", help="rebuild daily_cast_status from snapshots")
    p.add_argument("--date", type=_date_arg,
                   help="one business date (default: all)")
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("report", help="full analysis report")
    p.add_argument("--date", type=_date_arg,
                   help="focus date for area/shop/ranking sections "
                        "(default: latest target_date)")
    p.add_argument("--min-days", type=int, default=2,
                   help="alert: minimum prior observed days")
    p.add_argument("--threshold", type=float, default=0.6,
                   help="alert: sold-out rate over prior days")
    p.add_argument("--streak-min", type=int, default=2,
                   help="minimum consecutive sold-out days to list")
    p.add_argument("--top", type=int, default=15,
                   help="rows shown in area/shop sections")
    p.add_argument("--no-evidence", action="store_true",
                   help="report: omit alert evidence lines")
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("monitor",
                       help="coverage/health warnings for one business day"
                            " (exit 1 when warnings exist)")
    p.add_argument("--date", type=_date_arg,
                   help="business date (default: today's business date)")
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("watch", help="favorite casts: add/remove/list")
    p.add_argument("action", choices=("add", "remove", "list"))
    p.add_argument("--shop-id", type=int)
    p.add_argument("--cast-id", type=int)
    p.add_argument("--cast-name")
    p.add_argument("--note")
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("notify",
                       help="POST alert/forecast summary to the "
                            "ESTAMA_NOTIFY_WEBHOOK webhook")
    p.add_argument("--date", type=_date_arg,
                   help="business date (default: latest)")
    p.add_argument("--min-days", type=int, default=2)
    p.add_argument("--threshold", type=float, default=0.6)
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("dashboard",
                       help="write a self-contained HTML dashboard")
    p.add_argument("--date", type=_date_arg,
                   help="business date (default: latest)")
    p.add_argument("--out", default=str(BASE / "dashboard.html"))
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("cast",
                       help="one cast's profile: attendance, sell-out "
                            "hours, recent days")
    p.add_argument("--shop-id", type=int, required=True)
    p.add_argument("--cast-id", type=int)
    p.add_argument("--cast-name")
    p.add_argument("--db", default=str(BASE / "estama.db"))
    return parser


def _cmd_status(args):
    data, ids = load_watchlist(DEFAULT_SHOPS)
    if ids is None:
        print("watchlist: shops.json is missing or empty.")
    else:
        print(f"watchlist: {data.get('watchlist_name')} - "
              f"{len(ids)} shops configured")
    return 0


def _cmd_load(args):
    _, ids = load_watchlist(args.shops)
    if ids is None:
        print("WARNING: shops.json watchlist is empty. "
              "Loading without a watchlist filter.")
    conn = connect(args.db)
    stats = load_snapshot(conn, args.snapshot, watchlist_ids=ids)
    print(f"loaded into {args.db}: {stats}")
    print_verify(conn)
    return 0


def _cmd_sold_out(conn, args):
    rows = sold_out(conn, args.date)
    if not rows:
        print(f"{args.date}: no sold-out casts in DB")
    for shop, cast, shift, raw in rows:
        print(f"{shop} - {cast} (shift: {shift}) [{raw}]")
    return 0


def _cmd_counts(conn, args):
    for norm, n in status_counts(conn, args.date):
        print(f"{norm}: {n}")
    return 0


def _cmd_alert(conn, args):
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
        if not args.no_evidence:
            for line in alert_evidence_lines(
                    conn, shop, cast, args.date,
                    review_db=args.review_db,
                    bakusai_db=args.bakusai_db):
                print(line)
    return 0


def _cmd_verify(conn, args):
    print_verify(conn)
    return 0


def _cmd_rollup(conn, args):
    n = rollup_daily(conn, args.date)
    print(f"rollup: daily_cast_status {n} rows"
          + (f" (date {args.date})" if args.date else ""))
    return 0


def _cmd_sellout_forecast(conn, args):
    date = args.date or _expected_business_date(now_jst())
    deep = load_deep_shop_ids(args.deep_shops)
    for line in section_sellout_forecast(
            conn, date, at_hhmm=args.at, horizon=args.horizon,
            top=args.top, deep_shop_ids=deep):
        print(line)
    return 0


def _cmd_report(conn, args):
    print_report(conn, args.date, args.min_days, args.threshold,
                 args.streak_min, args.top,
                 evidence=not args.no_evidence)
    return 0


def _cmd_monitor(conn, args):
    dates = _all_dates(conn)
    if not dates:
        print("monitor: snapshots が空です")
        return 0
    date = args.date or _expected_business_date(now_jst())
    rep = monitor_report(conn, date)
    print("  " + rep["summary"])
    for w in rep["warnings"]:
        print(f"  ! {w}")
    if not rep["warnings"]:
        print("  異常なし")
    return 1 if rep["warnings"] else 0


def _cmd_watch(conn, args):
    if args.action == "list":
        rows = conn.execute(
            "SELECT shop_id, cast_key, cast_name, added_at, note"
            " FROM cast_watch ORDER BY added_at").fetchall()
        if not rows:
            print("cast_watch: empty")
        for sid, ck, cn, at, nt in rows:
            print(f"  shop={sid} key={ck} {cn} ({at})"
                  + (f" - {nt}" if nt else ""))
        return 0
    if args.shop_id is None or (args.cast_id is None
                                and args.cast_name is None):
        print("watch add/remove needs --shop-id and "
              "--cast-id or --cast-name")
        return 2
    if args.action == "add":
        ck, cname = watch_add(conn, args.shop_id, args.cast_id,
                              args.cast_name, args.note)
        print(f"watch added: shop={args.shop_id} key={ck} {cname}")
        return 0
    n = watch_remove(conn, args.shop_id, args.cast_id,
                     args.cast_name)
    print(f"watch removed: {n} row(s)")
    return 0 if n else 1


def _cmd_notify(conn, args):
    dates = _all_dates(conn)
    date = args.date or (dates[-1] if dates else
                         now_jst().date().isoformat())
    text = build_notify_text(conn, date, args.min_days,
                             args.threshold)
    print(text)
    if notify_webhook(text):
        print("notify: webhook posted")
    else:
        print("notify: no ESTAMA_NOTIFY_WEBHOOK set or POST failed "
              "(text printed above)")
    return 0


def _cmd_dashboard(conn, args):
    html = build_dashboard(conn, args.date)
    Path(args.out).write_text(html, encoding="utf-8")
    print(f"dashboard written: {args.out}")
    return 0


def _cmd_cast(conn, args):
    if args.cast_id is None and args.cast_name is None:
        print("cast needs --cast-id or --cast-name")
        return 2
    for line in section_cast_detail(conn, args.shop_id,
                                    args.cast_id, args.cast_name):
        print(line)
    return 0


_COMMANDS = {
    "sold-out": _cmd_sold_out,
    "counts": _cmd_counts,
    "alert": _cmd_alert,
    "verify": _cmd_verify,
    "rollup": _cmd_rollup,
    "sellout-forecast": _cmd_sellout_forecast,
    "report": _cmd_report,
    "monitor": _cmd_monitor,
    "watch": _cmd_watch,
    "notify": _cmd_notify,
    "dashboard": _cmd_dashboard,
    "cast": _cmd_cast,
}


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.cmd == "demo":
        return 0 if run_demo() else 1
    if args.cmd == "status":
        return _cmd_status(args)
    if args.cmd == "load":
        return _cmd_load(args)
    conn = connect(args.db)
    handler = _COMMANDS.get(args.cmd)
    return handler(conn, args) if handler else 1


if __name__ == "__main__":
    sys.exit(main())
