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
    python estama_tracker.py rollup [--date YYYY-MM-DD] [--db DB]
    python estama_tracker.py report [--date YYYY-MM-DD] [--db DB]
    python estama_tracker.py demo
    python estama_tracker.py verify [--db DB]
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from common import (BASE_DIR as BASE, SCHEMA_PATH as SCHEMA,
                    SHOPS_JSON_PATH as DEFAULT_SHOPS, connect_db, load_json,
                    normalize_status_text, now_jst)


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
# rows with no cast_id fall back to the name (demo/legacy rows);
# CAST(... AS TEXT) so an int cast_id and its text form can never
# split into two partitions.
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


def rollup_daily(conn, date=None):
    """Materialize the R3 effective final state into daily_cast_status.

    One row per (shop_id, cast_key, target_date); cast_key is
    CAST(COALESCE(cast_id, cast_name) AS TEXT), the same key every
    aggregation uses. Idempotent rebuild per day (delete + insert), so
    it is safe to call after every import - the runner does exactly
    that, making alert history, sold-out streaks and trends a plain
    table lookup instead of a window pass. Returns rows written.
    """
    if date:
        days = [date]
    else:
        days = [r[0] for r in conn.execute(
            "SELECT DISTINCT target_date FROM snapshots")]
    total = 0
    for d in days:
        rows = conn.execute(_final_sql("target_date = ?"), (d,)).fetchall()
        conn.execute(
            "DELETE FROM daily_cast_status WHERE target_date = ?", (d,))
        conn.executemany(
            """INSERT INTO daily_cast_status
               (shop_id, cast_key, cast_id, cast_name, target_date,
                status_norm, status_raw, work_hours, age, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(r[0], str(r[7] if r[7] is not None else r[1]), r[7], r[1],
              r[2], r[3], r[4], r[6], r[9], r[8]) for r in rows],
        )
        total += len(rows)
    conn.commit()
    return total


# ----------------------------------------------------------------- report ---
# `report` consolidates the ad-hoc analysis scripts (analyze_*.py) into
# one command. Every section is a function returning printable lines so
# tests can assert on content; print_report only formats.

def _all_dates(conn):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT target_date FROM snapshots ORDER BY target_date")]


def _expected_business_date(now):
    """Same rule as runner: the business date flips at ~06:00 JST."""
    from datetime import timedelta
    return (now - timedelta(hours=6)).date().isoformat()


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


def section_daily(conn, dates):
    """Per-day R3 final-state counts with sold-out rate."""
    lines = []
    expected = _expected_business_date(now_jst())
    for d in dates:
        rows = _day_finals(conn, d)
        c = Counter(r["status_norm"] for r in rows)
        n = sum(c.values())
        so = c.get("sold_out", 0)
        mark = "  <- 巡回中" if d == expected else ""
        lines.append(
            f"  {d}: casts={n:4d}  sold_out={so:3d} "
            f"({so / n * 100 if n else 0:4.1f}%)  low={c.get('low', 0):3d}  "
            f"available={c.get('available', 0):4d}  "
            f"closed={c.get('closed', 0):3d}  "
            f"dummy={c.get('dummy', 0):2d}  unknown={c.get('unknown', 0):2d}"
            f"{mark}")
    return lines


def section_streaks(conn, dates, streak_min=2):
    """Casts with >= streak_min consecutive sold-out days (latest first).

    The streak ends at the newest date; casts absent from the newest
    date still show their run with today's state '-'.
    """
    by_cast = {}
    names = {}
    for d in dates:
        for r in _day_finals(conn, d):
            k = (r["shop_id"], _ckey(r))
            by_cast.setdefault(k, {})[d] = r["status_norm"]
            names[k] = (r["cast_name"], r["cast_id"])
    lines = []
    for k, m in by_cast.items():
        streak = 0
        for d in reversed(dates):
            if m.get(d) == "sold_out":
                streak += 1
            else:
                break
        if streak < streak_min:
            continue
        today = m.get(dates[-1], "-")
        nm = names[k]
        lines.append((streak, today, k, nm))
    lines.sort(key=lambda x: (-x[0], x[1] != "sold_out", str(x[3][0])))
    return [
        f"  {st}日連続完売: {nm[0]} (cast_id={nm[1]}, shop={k[0]})  "
        f"最新日={today}"
        for st, today, k, nm in lines]


def section_timing(conn, dates):
    """First sold_out observation per cast-day, by hour (fetched_at)."""
    lines = []
    for d in dates:
        rows = conn.execute(
            """SELECT MIN(fetched_at) AS fa
               FROM snapshots
               WHERE target_date = ? AND status_norm = 'sold_out'
               GROUP BY shop_id,
                        CAST(COALESCE(cast_id, cast_name) AS TEXT)""",
            (d,)).fetchall()
        hist = Counter(r[0][11:13] for r in rows if r[0])
        late = sum(v for hh, v in hist.items() if hh < "06")
        top = ", ".join(f"{hh}時:{n}" for hh, n in
                        sorted(hist.items(), key=lambda x: -x[1])[:5])
        lines.append(
            f"  {d}: sold_out={len(rows):3d}人  深夜(00-05時)={late:3d}  "
            f"集中帯: {top}")
    return lines


def section_transitions(conn, dates):
    """Cast churn + R3 state-transition matrix between the last two days."""
    lines = []
    if len(dates) < 2:
        return lines + ["  (遷移分析には2営業日以上必要)"]
    a, b = dates[-2], dates[-1]
    prev = {(r["shop_id"], _ckey(r)): r["status_norm"]
            for r in _day_finals(conn, a)}
    curr = {(r["shop_id"], _ckey(r)): r["status_norm"]
            for r in _day_finals(conn, b)}
    both = set(prev) & set(curr)
    lines.append(
        f"  {a} -> {b}: 共通={len(both)}  消滅={len(prev) - len(both)}  "
        f"新規={len(curr) - len(both)}")
    trans = Counter((prev[k], curr[k]) for k in both)
    for (x, y), n in trans.most_common(10):
        lines.append(f"    {x:10s} -> {y:10s}  {n:4d}")
    return lines


def section_area(conn, date, top_n=15):
    """Sold-out rate by shops.area for one date (R3 final)."""
    area = {r[0]: (r[1] or "(未設定)") for r in conn.execute(
        "SELECT shop_id, area FROM shops")}
    agg = {}
    for r in _day_finals(conn, date):
        a = area.get(r["shop_id"], "(未設定)")
        s = agg.setdefault(a, {"so": 0, "cast": 0, "shops": set()})
        s["cast"] += 1
        s["shops"].add(r["shop_id"])
        if r["status_norm"] == "sold_out":
            s["so"] += 1
    ranked = sorted(agg.items(),
                    key=lambda x: -x[1]["so"] / max(x[1]["cast"], 1))
    return [
        f"  {a[:24]:24s} sold_out={s['so']:3d}/{s['cast']:4d} "
        f"({s['so'] / s['cast'] * 100:5.1f}%)  {len(s['shops'])}店"
        for a, s in ranked[:top_n]]


def section_shops(conn, date, top_n=15, min_casts=3):
    """Top shops by sold-out rate for one date (R3 final)."""
    name = {r[0]: r[1] for r in conn.execute(
        "SELECT shop_id, name FROM shops")}
    agg = {}
    for r in _day_finals(conn, date):
        s = agg.setdefault(r["shop_id"], {"so": 0, "cast": 0})
        s["cast"] += 1
        if r["status_norm"] == "sold_out":
            s["so"] += 1
    ranked = sorted(
        ((sid, s) for sid, s in agg.items() if s["cast"] >= min_casts),
        key=lambda x: -x[1]["so"] / x[1]["cast"])
    return [
        f"  {name.get(sid, '?')[:36]:36s} sold_out={s['so']:3d}/"
        f"{s['cast']:3d} ({s['so'] / s['cast'] * 100:5.1f}%)"
        for sid, s in ranked[:top_n]]


def section_ranking(conn, date):
    """Coverage + correlation of the latest weekly ranking.

    Coverage is the real bottleneck found in the 10-27 analysis:
    ranked casts on shops missing from shops.json are never observed,
    so the report lists the worst gaps as an actionable watchlist diff.
    """
    lines = []
    row = conn.execute(
        "SELECT MAX(fetched_date) FROM cast_rankings").fetchone()
    if not row or not row[0]:
        return ["  (cast_rankings なし)"]
    rd = row[0]
    lines.append(f"  ranking fetched_date: {rd}")
    ranked = {r[0] for r in conn.execute(
        "SELECT DISTINCT cast_id FROM cast_rankings "
        "WHERE period_type='weekly' AND fetched_date=?", (rd,))}
    top10 = {r[0] for r in conn.execute(
        "SELECT DISTINCT cast_id FROM cast_rankings "
        "WHERE period_type='weekly' AND fetched_date=? AND rank<=10",
        (rd,))}
    finals = _day_finals(conn, date)
    obs = {r["cast_id"] for r in finals if r["cast_id"] is not None}
    so = {r["cast_id"] for r in finals
          if r["status_norm"] == "sold_out" and r["cast_id"] is not None}
    if ranked:
        lines.append(
            f"  掲載 cast={len(ranked)} / 観測={len(obs & ranked)} "
            f"({len(obs & ranked) / len(ranked) * 100:.0f}%)  "
            f"sold_out={len(so & ranked)}")
        lines.append(
            f"  top10 cast={len(top10)} / 観測={len(top10 & obs)}  "
            f"sold_out={len(top10 & so)}")
        rest = obs - top10
        top_rate = (len(top10 & so) / len(top10 & obs)
                    if top10 & obs else 0)
        rest_rate = len(rest & so) / len(rest) if rest else 0
        lines.append(
            f"  sold_out率  top10観測分={top_rate * 100:.0f}%  vs  "
            f"その他={rest_rate * 100:.0f}%")
    unreg = conn.execute(
        """SELECT cr.shop_id, cr.shop_name, COUNT(*) AS n, MIN(cr.rank) AS best
           FROM cast_rankings cr
           WHERE cr.fetched_date = ?
             AND cr.shop_id NOT IN (SELECT shop_id FROM shops)
           GROUP BY cr.shop_id ORDER BY best, n DESC LIMIT 10""",
        (rd,)).fetchall()
    if unreg:
        lines.append("  未登録店（ランキング掲載だが巡回対象外）:")
        for sid, nm, n, best in unreg:
            lines.append(
                f"    {sid} {nm[:34]:34s} ranked={n}人 best_rank={best}")
    return lines


def section_consistency(conn):
    """Rename detection (one cast_id, several names) + NULL coverage."""
    lines = []
    ren = conn.execute(
        """SELECT shop_id, cast_id, COUNT(DISTINCT cast_name) n,
                  GROUP_CONCAT(DISTINCT cast_name)
           FROM snapshots WHERE cast_id IS NOT NULL
           GROUP BY shop_id, cast_id HAVING n > 1""").fetchall()
    lines.append(f"  同cast_id→複数名（改名）: {len(ren)}組")
    for sid, cid, n, names in ren[:10]:
        lines.append(f"    shop={sid} id={cid} {names}")
    tot = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    if tot:
        lines.append("  NULL率:")
        for fld in ("cast_id", "age", "shift", "work_minutes",
                    "status_raw", "fetched_at"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM snapshots WHERE {fld} IS NULL"
            ).fetchone()[0]
            lines.append(f"    {fld:14s} {n:6d} ({n / tot * 100:4.1f}%)")
    return lines


def section_runlog(conn, limit=8):
    """Tail of the runner job log, when the table has rows."""
    try:
        rows = conn.execute(
            """SELECT job, started_at, status, detail FROM run_log
               ORDER BY run_id DESC LIMIT ?""", (limit,)).fetchall()
    except sqlite3.OperationalError:
        return ["  (run_log なし)"]
    if not rows:
        return ["  (run_log なし)"]
    return [
        f"  {st} {job:10s} {status:7s} {detail or ''}"
        for job, st, status, detail in rows]


def print_report(conn, date=None, min_days=2, threshold=0.6,
                 streak_min=2, top_n=15):
    dates = _all_dates(conn)
    if not dates:
        print("report: snapshots が空です")
        return
    date = date or dates[-1]

    def head(t):
        print("\n" + "=" * 70)
        print(t)
        print("=" * 70)

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
    for shop, cast, norm, raw, n_sold, n, rate in alerts:
        print(f"  ALERT {shop} - {cast}: {norm} [{raw}] "
              f"(完売 {n_sold}/{n}日, {rate})")
    head("【4】完売の初観測タイミング")
    for line in section_timing(conn, dates):
        print(line)
    head("【5】キャスト出入り・状態遷移")
    for line in section_transitions(conn, dates):
        print(line)
    head(f"【6】エリア別 sold_out 率（{date}）")
    for line in section_area(conn, date, top_n):
        print(line)
    head(f"【7】店舗別 sold_out 率 上位（{date}, 3名以上の店）")
    for line in section_shops(conn, date, top_n):
        print(line)
    head(f"【8】週間ランキング照合（{date}）")
    for line in section_ranking(conn, date):
        print(line)
    head("【9】cast整合性・データ品質")
    for line in section_consistency(conn):
        print(line)
    head("【10】runner ジョブ履歴")
    for line in section_runlog(conn):
        print(line)


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
    p = sub.add_parser("rollup", help="rebuild daily_cast_status from snapshots")
    p.add_argument("--date", help="one business date (default: all)")
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("report", help="full analysis report")
    p.add_argument("--date", help="focus date for area/shop/ranking sections "
                                 "(default: latest target_date)")
    p.add_argument("--min-days", type=int, default=2,
                   help="alert: minimum prior observed days")
    p.add_argument("--threshold", type=float, default=0.6,
                   help="alert: sold-out rate over prior days")
    p.add_argument("--streak-min", type=int, default=2,
                   help="minimum consecutive sold-out days to list")
    p.add_argument("--top", type=int, default=15,
                   help="rows shown in area/shop sections")
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
    if args.cmd == "rollup":
        n = rollup_daily(conn, args.date)
        print(f"rollup: daily_cast_status {n} rows"
              + (f" (date {args.date})" if args.date else ""))
        return 0
    if args.cmd == "report":
        print_report(conn, args.date, args.min_days, args.threshold,
                     args.streak_min, args.top)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
