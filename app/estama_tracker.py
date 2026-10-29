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
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from common import (BASE_DIR as BASE, SCHEMA_PATH as SCHEMA,
                    SHOPS_JSON_PATH as DEFAULT_SHOPS, connect_db, load_json,
                    normalize_status_text, notify_webhook, now_jst)


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

    # repeat sellers: first-observed sold_out hour across days
    per_cast = conn.execute(
        """SELECT shop_id, cast_name,
                  CAST(COALESCE(cast_id, cast_name) AS TEXT) k,
                  target_date, MIN(fetched_at)
           FROM snapshots WHERE status_norm = 'sold_out'
           GROUP BY 1, 3, 4""").fetchall()
    by_cast = {}
    for sid, name, k, d, fa in per_cast:
        by_cast.setdefault((sid, name, k), {})[d] = fa[11:13]
    names = {s: n for s, n in
             conn.execute("SELECT shop_id, name FROM shops")}
    regulars = sorted(
        ((len(v), kk, v) for kk, v in by_cast.items() if len(v) >= 2),
        key=lambda x: -x[0])
    if regulars:
        lines.append("  常連の完売時刻（2日以上完売, HH=初観測時）:")
        for n, (sid, name, _), hours in regulars[:10]:
            hh = ",".join(f"{d[5:]}={h}" for d, h in sorted(hours.items()))
            lines.append(f"    {names.get(sid, sid)} - {name}: {hh}")
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


def section_forecast(conn, date, min_days=2, limit=20):
    """Sell-out candidates for `date`: casts whose latest observation is
    'low' (残りわずか) and whose history says they sell out often, plus
    the leading-indicator stat (how often 'low' precedes sold_out)."""
    lines = []

    # leading indicator: share of sold_out cast-days that saw 'low'
    # at least once before the winning sold_out observation
    tot, with_low = conn.execute(
        """WITH f AS (
             SELECT shop_id, CAST(COALESCE(cast_id, cast_name) AS TEXT) k,
                    target_date, status_norm,
                    MIN(CASE WHEN status_norm='sold_out' THEN fetched_at END)
                      first_so
             FROM snapshots GROUP BY 1,2,3)
           SELECT COUNT(*),
                  SUM(CASE WHEN first_so IS NOT NULL AND EXISTS(
                        SELECT 1 FROM snapshots s
                        WHERE s.shop_id = f.shop_id
                          AND CAST(COALESCE(s.cast_id, s.cast_name) AS TEXT) = f.k
                          AND s.target_date = f.target_date
                          AND s.status_norm = 'low'
                          AND s.fetched_at < f.first_so)
                      THEN 1 ELSE 0 END)
           FROM f WHERE first_so IS NOT NULL""").fetchone()
    if tot:
        lines.append(
            f"  完売cast-dayの {with_low}/{tot} "
            f"({with_low / tot * 100:.0f}%) は完売前に「残りわずか」を観測"
            "  <- この値が高いほど low は信頼できる予兆")

    # today's 'low' casts, ranked by their sold-out track record
    finals = {d: _day_finals(conn, d) for d in _all_dates(conn)}
    hist = {}
    for d, rows in finals.items():
        if d >= date:
            continue
        for r in rows:
            h = hist.setdefault((r["shop_id"], _ckey(r)), [0, 0])
            h[1] += 1
            h[0] += r["status_norm"] == "sold_out"
    cands = []
    for r in finals.get(date, []):
        if r["status_norm"] != "low":
            continue
        so, n = hist.get((r["shop_id"], _ckey(r)), [0, 0])
        if n >= min_days and so >= 1:
            cands.append((so / n, so, n, r))
    cands.sort(key=lambda x: (-x[0], -x[1]))
    shop_names = {sid: name for sid, name in
                  conn.execute("SELECT shop_id, name FROM shops")}
    for rate, so, n, r in cands[:limit]:
        lines.append(
            f"  予兆 {shop_names.get(r['shop_id'], r['shop_id'])}"
            f" - {r['cast_name']}: [{r['status_raw']}]"
            f" (過去完売 {so}/{n}日, {rate:.0%})")
    if not cands:
        lines.append(f"  (履歴{min_days}日以上かつ現在 low のcastなし)")

    # self-scoring trend written by rollup into forecast_eval
    try:
        ev = conn.execute(
            """SELECT target_date, candidates, hits FROM forecast_eval
               ORDER BY target_date""").fetchall()
    except sqlite3.OperationalError:
        ev = []
    if ev:
        tc = sum(r[1] for r in ev)
        th = sum(r[2] for r in ev)
        lines.append(
            f"  採点: low→完売 的中 {th}/{tc} "
            f"({th / tc * 100:.0f}%)" if tc else "  採点データなし")
        for d, c, h in ev[-5:]:
            lines.append(
                f"    {d}: {h}/{c}"
                + (f" ({h / c * 100:.0f}%)" if c else ""))
    return lines


def section_rank_moves(conn, period_type="weekly", min_delta=5, limit=15):
    """Rank movement between the two latest ranking snapshots.

    Uses area 2700 (Osaka overall) - per-area boards duplicate the
    same casts and would report the same move up to 14 times.
    'New' = entered top-30 since the previous fetch, 'out' = dropped.
    """
    try:
        dates = [r[0] for r in conn.execute(
            """SELECT DISTINCT fetched_date FROM cast_rankings
               WHERE area_code = '2700' AND period_type = ?
               ORDER BY fetched_date DESC LIMIT 2""",
            (period_type,)).fetchall()]
    except sqlite3.OperationalError:
        dates = []
    if len(dates) < 2:
        return ["  (比較可能なランキングスナップが2日分未満)"]
    new_d, old_d = dates

    def ranks(d):
        return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            """SELECT cast_id, rank, cast_name, shop_name
               FROM cast_rankings
               WHERE area_code = '2700' AND period_type = ?
                 AND fetched_date = ?""",
            (period_type, d)).fetchall()}

    now, prev = ranks(new_d), ranks(old_d)
    lines = [f"  {old_d} -> {new_d} 変動:"]
    moved = sorted(
        ((prev[c][0] - now[c][0], c, now[c]) for c in now.keys() & prev.keys()
         if abs(prev[c][0] - now[c][0]) >= min_delta),
        key=lambda x: x[0])
    # risers and fallers each get a share of the limit so a day of many
    # drops can't crowd every riser out of the report
    ups = [m for m in moved if m[0] > 0][:limit // 2]
    downs = [m for m in moved if m[0] < 0][:limit - len(ups)]
    for delta, cid, (rk, nm, shop) in ups + downs:
        lines.append(
            f"    {'↑' if delta > 0 else '↓'} {nm} ({shop[:16]}): "
            f"{prev[cid][0]}位 -> {rk}位")
    entered = [c for c in now.keys() - prev.keys()]
    dropped = [c for c in prev.keys() - now.keys()]
    if entered:
        lines.append(f"    新規IN {len(entered)}人: "
                     + ", ".join(now[c][1] for c in sorted(
                         entered, key=lambda c: now[c][0])))
    if dropped:
        lines.append(f"    圏外OUT {len(dropped)}人: "
                     + ", ".join(prev[c][1] for c in sorted(
                         dropped, key=lambda c: prev[c][0])))
    if not moved and not entered and not dropped:
        lines.append("    変動なし")
    return lines


_WD = "日月火水木金土"


def section_watch(conn, date):
    """Favorite casts (cast_watch): today's state + weekday attendance.

    Weekday tendency comes from daily_cast_status (R3 finals), so a
    'present' day = the cast has any final row that day.
    """
    try:
        watch = conn.execute(
            """SELECT w.shop_id, w.cast_key, w.cast_name, s.name
               FROM cast_watch w LEFT JOIN shops s USING (shop_id)
               ORDER BY w.added_at""").fetchall()
    except sqlite3.OperationalError:
        watch = []
    if not watch:
        return ["  (cast_watch なし - 'watch add' で登録)"]
    lines = []
    today = {f"{r['shop_id']}|{_ckey(r)}": r for r in _day_finals(conn, date)}
    mark = {"sold_out": "完売", "low": "残少", "available": "空き",
            "closed": "終了", "dummy": "ダミー", "unknown": "不明"}
    for sid, ck, cname, shop in watch:
        r = today.get(f"{sid}|{ck}")
        st = mark.get(r["status_norm"], r["status_norm"]) if r else "休み?"
        extra = ""
        if r and r["status_norm"] == "sold_out":
            fa = conn.execute(
                """SELECT first_sold_out_at FROM daily_cast_status
                   WHERE shop_id = ? AND cast_key = ? AND target_date = ?""",
                (sid, ck, date)).fetchone()
            if fa and fa[0]:
                extra = f" ({fa[0][11:16]}時点で初観測)"
        # weekday attendance: share of each weekday the cast appeared
        days = conn.execute(
            """SELECT DISTINCT target_date FROM daily_cast_status
               WHERE shop_id = ? AND cast_key = ?""",
            (sid, ck)).fetchall()
        wd = Counter()
        for (d,) in days:
            wd[datetime.fromisoformat(d).weekday()] += 1
        # Python weekday(): Mon=0..Sun=6; _WD[0]=日 so map via (i+1)%7
        wdtxt = " ".join(f"{_WD[(i + 1) % 7]}{wd.get(i, 0)}"
                         for i in range(7) if wd.get(i, 0))
        lines.append(f"  {shop or sid} - {cname}: 今日={st}{extra}  "
                     f"出勤日傾向: {wdtxt or 'データなし'}")
    return lines


_SLOT_RE = re.compile(r"○(\d+)コマ / ×(\d+)コマ")


def _slot_counts(slot_pattern):
    """(open, booked) from '○Nコマ / ×Mコマ (予約可)'; None if other shape."""
    if not slot_pattern:
        return None
    m = _SLOT_RE.search(slot_pattern)
    return (int(m.group(1)), int(m.group(2))) if m else None


def section_momentum(conn, date, min_booked=4, limit=15):
    """Booking momentum today: casts whose × (booked slot) count grew
    without reaching sold_out yet - the 'reservations are flowing in'
    signal that fires before 'low' does.

    booked = x_latest - x_first. When × never appears (slots vanish
    at shift end without a × tally), fall back to o_first - o_last.
    """
    rows = conn.execute(
        """SELECT shop_id, CAST(COALESCE(cast_id, cast_name) AS TEXT),
                  MAX(cast_name), slot_pattern, fetched_at, status_norm
           FROM snapshots WHERE target_date = ?
           GROUP BY 1, 2, fetched_at
           ORDER BY shop_id, cast_id, fetched_at""", (date,)).fetchall()
    per = {}
    for sid, ck, cname, sp, fa, norm in rows:
        ent = per.setdefault((sid, ck), {"name": cname, "norm": norm,
                                        "first": None, "last": None,
                                        "fa0": fa, "fa1": fa})
        ent["fa1"] = fa
        ent["norm"] = norm  # last observation wins (groups keep order)
        sc = _slot_counts(sp)
        if sc is None:
            continue
        if ent["first"] is None:
            ent["first"], ent["fa0"] = sc, fa
        ent["last"], ent["fa1"] = sc, fa
    names = {s: n for s, n in
             conn.execute("SELECT shop_id, name FROM shops")}
    # R3 final states: a cast that sold out and then flipped to 'closed'
    # at shift end must count as sold_out, not as 'still bookable'
    sold_out_finals = {(r["shop_id"], _ckey(r))
                       for r in _day_finals(conn, date)
                       if r["status_norm"] == "sold_out"}
    cands = []
    for (sid, ck), e in per.items():
        if e["norm"] == "sold_out" or (sid, ck) in sold_out_finals \
                or e["first"] is None:
            continue
        o0, x0 = e["first"]
        o1, x1 = e["last"]
        booked = x1 - x0
        if booked <= 0:
            booked = max(0, o0 - o1)
        if booked < min_booked:
            continue
        hrs = max(0.25, (datetime.fromisoformat(
            e["fa1"].replace(" JST", ""))
            - datetime.fromisoformat(
                e["fa0"].replace(" JST", ""))).total_seconds() / 3600)
        cands.append((booked / hrs, booked, o1, sid, e["name"], e["norm"]))
    cands.sort(key=lambda x: -x[0])
    lines = []
    for rate, booked, left, sid, cname, norm in cands[:limit]:
        lines.append(
            f"  勢い {names.get(sid, sid)} - {cname}: "
            f"{booked}枠消化 (残{left}枠, {rate:.1f}枠/h) [{norm}]")
    if not cands:
        lines.append(f"  ({min_booked}枠以上の消化なし)")
    return lines


def section_new_casts(conn, date, transfer_days=14):
    """New cast_ids first observed on `date` + cast_ids seen under
    more than one shop in the recent window (possible transfers)."""
    lines = []
    first_seen = dict(conn.execute(
        """SELECT CAST(COALESCE(cast_id, cast_name) AS TEXT),
                  MIN(target_date) FROM snapshots GROUP BY 1"""))
    # one row per (shop, cast_key); a mid-day rename must not count
    # the same cast_id twice - keep the latest observed name
    latest = {}
    for sid, cname, ck in conn.execute(
            """SELECT s.shop_id, s.cast_name,
                      CAST(COALESCE(s.cast_id, s.cast_name) AS TEXT)
               FROM snapshots s WHERE s.target_date = ?
               ORDER BY s.fetched_at""", (date,)).fetchall():
        latest[(sid, ck)] = cname
    newc = [(sid, cname, ck) for (sid, ck), cname in latest.items()
            if first_seen.get(ck) == date]
    names = {s: n for s, n in
             conn.execute("SELECT shop_id, name FROM shops")}
    if newc:
        lines.append(f"  新規cast（{date} 初登場）: {len(newc)}人")
        for sid, cname, cid in sorted(newc,
                                      key=lambda x: names.get(x[0], ""))[:15]:
            lines.append(f"    {names.get(sid, sid)} - {cname}")
        if len(newc) > 15:
            lines.append(f"    ...他 {len(newc) - 15}人")
    else:
        lines.append(f"  新規cast: {date} の初登場なし")
    # transfers: same cast_id under 2+ shops inside the window
    cutoff = (datetime.fromisoformat(date)
              - timedelta(days=transfer_days)).date().isoformat()
    tr = conn.execute(
        """SELECT cast_id, GROUP_CONCAT(DISTINCT shop_id),
                  COUNT(DISTINCT shop_id) n, MAX(cast_name)
           FROM snapshots
           WHERE cast_id IS NOT NULL AND target_date >= ?
           GROUP BY cast_id HAVING n > 1""", (cutoff,)).fetchall()
    if tr:
        lines.append(f"  複数店舗に出現（直近{transfer_days}日, 移籍?）:")
        for cid, sids, n, cname in tr[:10]:
            lines.append(f"    {cname} (id={cid}): "
                         + ", ".join(names.get(int(s), s)
                                     for s in sids.split(",")))
    return lines


def section_cast_detail(conn, shop_id, cast_id=None, cast_name=None,
                        last_days=14):
    """One cast's full profile: weekday attendance, sell-out hours,
    recent per-day states. `cast show` prints this."""
    ck, cname = _resolve_cast(conn, shop_id, cast_id, cast_name)
    hist = conn.execute(
        """SELECT target_date, status_norm, first_sold_out_at, work_hours
           FROM daily_cast_status
           WHERE shop_id = ? AND cast_key = ?
           ORDER BY target_date""", (shop_id, ck)).fetchall()
    if not hist:
        return [f"  {cname}: daily_cast_status にデータなし"
                "（先に rollup を実行）"]
    shop = conn.execute(
        "SELECT name FROM shops WHERE shop_id = ?", (shop_id,)).fetchone()
    lines = [f"  {shop[0] if shop else shop_id} - {cname} (key={ck})"]
    wd = Counter(datetime.fromisoformat(d).weekday()
                 for d, *_ in hist)
    wdtxt = " ".join(f"{_WD[(i + 1) % 7]}: {wd.get(i, 0)}日"
                     for i in range(7) if wd.get(i, 0))
    lines.append(f"  出勤曜日傾向: {wdtxt}")
    so_hours = sorted(int(fa[11:13])
                      for _, st, fa, _ in hist
                      if st == "sold_out" and fa)
    if so_hours:
        import statistics
        lines.append(
            f"  完売時刻（初観測）: {', '.join(f'{h:02d}時' for h in so_hours)}"
            f"  中央値 {statistics.median(so_hours):.0f}時")
    lines.append("  直近の日別:")
    mark = {"sold_out": "完売", "low": "残少", "available": "空き",
            "closed": "終了", "dummy": "ダミー", "unknown": "不明"}
    for d, st, fa, wh in hist[-last_days:]:
        t = f" {fa[11:16]}完売" if st == "sold_out" and fa else ""
        lines.append(f"    {d} {mark.get(st, st)}{t}")
    n = len(hist)
    so = sum(1 for _, st, _, _ in hist if st == "sold_out")
    lines.append(f"  累計: {n}日出勤, 完売 {so}日 ({so / n * 100:.0f}%)")
    return lines


def _low_coverage_shops(conn, date):
    """{shop_id: scrape-hours} for shops covered in anomalously few hours
    on `date` (<40% of the per-shop median). Alerts from these shops may
    be coverage artifacts, not real availability changes."""
    per = conn.execute(
        """SELECT shop_id, COUNT(DISTINCT substr(fetched_at, 12, 2))
           FROM snapshots WHERE target_date = ? GROUP BY 1""",
        (date,)).fetchall()
    if not per:
        return {}
    med = sorted(h for _, h in per)[len(per) // 2]
    return {s: h for s, h in per if h < max(2, med * 0.4)}


def section_coverage(conn, dates):
    """Scrape coverage audit: missing checkpoint hours, partial runs,
    shops with anomalously low coverage on the latest day, and
    duplicate scrapes within one hour."""
    lines = []
    for d in dates:
        tot = conn.execute(
            "SELECT COUNT(DISTINCT shop_id) FROM snapshots"
            " WHERE target_date = ?", (d,)).fetchone()[0]
        cov = dict(conn.execute(
            """SELECT substr(fetched_at, 12, 2), COUNT(DISTINCT shop_id)
               FROM snapshots WHERE target_date = ?
               GROUP BY 1""", (d,)).fetchall())
        # business-day hour order: 06..23 (day d) then 00..05 (day d+1)
        grid = [f"{h:02d}" for h in list(range(6, 24)) + list(range(0, 6))]
        seen = [h for h in grid if h in cov]
        if not seen:
            continue
        first, last = seen[0], seen[-1]
        missing = [h for h in grid[grid.index(first):grid.index(last) + 1]
                   if h not in cov]
        partial = [(h, n) for h, n in cov.items() if n < tot * 0.9]
        dup = conn.execute(
            """SELECT COUNT(*) FROM (
                 SELECT 1 FROM snapshots WHERE target_date = ?
                 GROUP BY shop_id, COALESCE(cast_id, cast_name),
                          substr(fetched_at, 1, 13)
                 HAVING COUNT(*) > 1)""", (d,)).fetchone()[0]
        msg = f"  {d}: shops={tot}  取得時間帯={len(cov)}/{len(seen) + len(missing)}"
        if missing:
            msg += f"  未取得時間={','.join(missing)}時"
        lines.append(msg)
        for h, n in sorted(partial):
            lines.append(f"    {h}時: {n}/{tot}店のみ "
                         "(途中停止の可能性)")
        if dup:
            lines.append(f"    同一時間帯の重複巡回: {dup}件")

    # shops with anomalously few scrape-hours on the latest day
    d = dates[-1]
    stale = _low_coverage_shops(conn, d)
    if stale:
        names = {s: n for s, n in
                 conn.execute("SELECT shop_id, name FROM shops")}
        lines.append(f"  {d} にほぼ取得できていない店:")
        for s, h in sorted(stale.items(), key=lambda x: x[1]):
            lines.append(f"    {s} {names.get(s, '?')}: {h}時間のみ")
    return lines


def section_runlog(conn, limit=8):
    """Tail of the runner job log + per-shop scrape outcomes, when present."""
    lines = []
    try:
        rows = conn.execute(
            """SELECT job, started_at, status, detail FROM run_log
               ORDER BY run_id DESC LIMIT ?""", (limit,)).fetchall()
        stale = conn.execute(
            """SELECT job, started_at FROM run_log
               WHERE status = 'running'
                 AND started_at < ?""",
            ((now_jst() - timedelta(hours=2))
             .strftime("%Y-%m-%d %H:%M:%S") + " JST",)).fetchall()
    except sqlite3.OperationalError:
        rows, stale = [], []
    for job, st in stale:
        lines.append(f"  !! 滞留run: {job} started {st} "
                     "- 2時間超 running。runner 死亡の可能性")
    lines += [
        f"  {st} {job:10s} {status:7s} {detail or ''}"
        for job, st, status, detail in rows] or ["  (run_log なし)"]

    # per-shop scrape audit: hourly status counts for the latest day that
    # has rows + the most recent fail/empty shops
    try:
        latest = conn.execute(
            "SELECT substr(fetched_at,1,10) FROM scrape_log"
            " ORDER BY log_id DESC LIMIT 1").fetchone()
        if not latest:
            return lines
        hourly = conn.execute(
            """SELECT substr(fetched_at,12,2) hh, status, COUNT(*)
               FROM scrape_log WHERE substr(fetched_at,1,10)=?
               GROUP BY 1,2 ORDER BY 1,2""", (latest[0],)).fetchall()
        bad = conn.execute(
            """SELECT substr(fetched_at,12,19), shop_id, status,
                      substr(detail,1,40)
               FROM scrape_log WHERE status!='ok'
               ORDER BY log_id DESC LIMIT 10""").fetchall()
    except sqlite3.OperationalError:
        return lines
    lines.append(f"  scrape_log {latest[0]} (ok/empty/fail per hour):")
    hourmap = {}
    for hh, st, n in hourly:
        hourmap.setdefault(hh, {})[st] = n
    for hh in sorted(hourmap):
        s = hourmap[hh]
        lines.append(f"    {hh}時 ok={s.get('ok', 0)} empty={s.get('empty', 0)}"
                     f" fail={s.get('fail', 0)}")
    if bad:
        lines.append("  直近の empty/fail:")
        lines += [f"    {fa} shop={sid} {st} {dt or ''}"
                  for fa, sid, st, dt in bad]
    return lines


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
    low_cov = _low_coverage_shops(conn, date)
    name2sid = {n: s for s, n in
                conn.execute("SELECT shop_id, name FROM shops")}
    for shop, cast, norm, raw, n_sold, n, rate in alerts:
        gap = "  <<巡回欠損:この店の今日の取得が少ない>>" \
            if name2sid.get(shop) in low_cov else ""
        print(f"  ALERT {shop} - {cast}: {norm} [{raw}] "
              f"(完売 {n_sold}/{n}日, {rate}){gap}")
    head(f"【4】完売予兆（{date} 現在 low の注目cast）")
    for line in section_forecast(conn, date, min_days):
        print(line)
    head(f"【4b】予約の勢い（{date} 枠消化トップ）")
    for line in section_momentum(conn, date):
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


# ------------------------------------------------------ watch / notify ----

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


def build_notify_text(conn, date, min_days=2, threshold=0.6, limit=8):
    """Notification body: alerts + forecast + rank moves + watch hits.

    Short by design - the report/dashboard carry the detail; the push
    only needs 'what changed that I should open the site for'."""
    parts = [f"[estama] {date} レポート"]
    alerts, _ = usually_sold_out_alert(conn, date, min_days, threshold)
    if alerts:
        parts.append("■ 普段完売なのに空き:")
        for shop, cast, norm, raw, n_sold, n, rate in alerts[:limit]:
            parts.append(f"  {shop} - {cast} [{raw}] (完売率 {rate:.0%})")
    try:
        watch = conn.execute(
            """SELECT w.shop_id, w.cast_key, w.cast_name FROM cast_watch w
            """).fetchall()
    except sqlite3.OperationalError:
        watch = []
    if watch:
        finals = {f"{r['shop_id']}|{_ckey(r)}": r
                  for r in _day_finals(conn, date)}
        hits, sold = [], []
        for sid, ck, cname in watch:
            r = finals.get(f"{sid}|{ck}")
            if not r:
                continue
            if r["status_norm"] in ("available", "low"):
                hits.append(f"  {cname}: [{r['status_raw']}]")
            elif r["status_norm"] == "sold_out":
                sold.append(f"  {cname}")
        if hits:
            parts.append("■ ウォッチ中のcastが空き/残少:")
            parts += hits[:limit]
        if sold:
            parts.append("■ ウォッチ中のcastが完売:")
            parts += sold[:limit]
    try:
        ev = conn.execute(
            """SELECT candidates, hits FROM forecast_eval
               WHERE target_date = ?""", (date,)).fetchone()
    except sqlite3.OperationalError:
        ev = None
    if ev and ev[0]:
        parts.append(f"■ 本日の予兆採点(速報): low→完売 {ev[1]}/{ev[0]}")
    moves = [l for l in section_rank_moves(conn)
             if any(t in l for t in ("↑", "↓", "新規IN", "圏外OUT"))]
    if moves:
        parts.append("■ ランキング変動:")
        parts += moves[:8]
        if len(moves) > 8:
            parts.append(f"  ...他 {len(moves) - 8}件")
    mom = [l for l in section_momentum(conn, date) if "勢い" in l]
    if mom:
        parts.append("■ 予約の勢い（枠消化中）:")
        parts += [l.strip() for l in mom[:5]]
    news = [l.strip() for l in section_new_casts(conn, date)
            if l.strip().startswith("新規cast（") or l.strip().startswith("複数店舗")]
    if news:
        parts += news
    try:
        fails = conn.execute(
            """SELECT job, started_at, detail FROM run_log
               WHERE status = 'fail' ORDER BY run_id DESC LIMIT 3"""
        ).fetchall()
    except sqlite3.OperationalError:
        fails = []
    if fails:
        parts.append("■ runner失敗:")
        parts += [f"  {j} {st} {dt or ''}" for j, st, dt in fails]
    if len(parts) == 1:
        parts.append("特記事項なし")
    return "\n".join(parts)


# ------------------------------------------------------------ dashboard ----

_DASH_CSS = """
* { box-sizing: border-box; margin: 0; }
body { font-family: -apple-system, "Hiragino Sans", sans-serif;
       background: #0f1420; color: #e8ecf4; padding: 16px;
       max-width: 860px; margin: 0 auto; }
h1 { font-size: 20px; margin-bottom: 4px; }
.sub { color: #8b93a7; font-size: 12px; margin-bottom: 16px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
         gap: 8px; margin-bottom: 20px; }
.card { background: #1a2133; border-radius: 10px; padding: 12px; text-align: center; }
.card .n { font-size: 26px; font-weight: 700; }
.card .l { font-size: 11px; color: #8b93a7; margin-top: 2px; }
.c-sold .n { color: #ff6b81; } .c-low .n { color: #f5c451; }
.c-avail .n { color: #4ade80; } .c-closed .n { color: #8b93a7; }
section { background: #1a2133; border-radius: 10px; padding: 14px 16px;
          margin-bottom: 14px; }
h2 { font-size: 14px; margin-bottom: 10px; color: #aab4cf; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
td, th { padding: 6px 8px; text-align: left; border-bottom: 1px solid #2a3350; }
th { color: #8b93a7; font-weight: 600; font-size: 11px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 99px;
         font-size: 11px; font-weight: 600; }
.b-sold { background: #4a1f2b; color: #ff8fa0; }
.b-low { background: #4a3a1f; color: #f5c451; }
.b-avail { background: #1f4a2e; color: #4ade80; }
.b-closed { background: #2a3350; color: #8b93a7; }
.b-unknown, .b-dummy { background: #2a3350; color: #aab4cf; }
.warn { color: #f5c451; }
.mut { color: #8b93a7; font-size: 12px; }
"""


def _badge(norm):
    label = {"sold_out": "完売", "low": "残少", "available": "空き",
             "closed": "終了", "dummy": "ダミー", "unknown": "不明"
             }.get(norm, norm)
    cls = {"sold_out": "b-sold", "low": "b-low", "available": "b-avail",
           "closed": "b-closed"}.get(norm, "b-unknown")
    return f'<span class="badge {cls}">{label}</span>'


def build_dashboard(conn, date=None, min_days=2, threshold=0.6):
    """Self-contained HTML dashboard. No JS, no external assets."""
    import html
    esc = html.escape
    dates = _all_dates(conn)
    date = date or (dates[-1] if dates else now_jst().date().isoformat())
    counts = dict(status_counts(conn, date))
    finals = _day_finals(conn, date)
    names = {s: n for s, n in
             conn.execute("SELECT shop_id, name FROM shops")}

    cards = "".join(
        f'<div class="card c-{k}"><div class="n">{counts.get(k, 0)}</div>'
        f'<div class="l">{label}</div></div>'
        for k, label in [("sold_out", "完売"), ("low", "残りわずか"),
                         ("available", "空き"), ("closed", "受付終了"),
                         ("dummy", "ダミー"), ("unknown", "不明")])

    def rows_of(status):
        return [r for r in finals if r["status_norm"] == status]

    def table(status, cols=True):
        rs = rows_of(status)
        if not rs:
            return f'<p class="mut">該当なし</p>'
        body = "".join(
            f"<tr><td>{esc(names.get(r['shop_id'], str(r['shop_id'])))}</td>"
            f"<td>{esc(r['cast_name'])}</td>"
            f"<td>{esc(str(r['shift'] or ''))}</td>"
            f"<td>{_badge(r['status_norm'])}</td></tr>"
            for r in sorted(rs, key=lambda r: (names.get(
                r['shop_id'], ''), r['cast_name'])))
        return ("<table><tr><th>店舗</th><th>キャスト</th><th>シフト</th>"
                f"<th>状態</th></tr>{body}</table>")

    alerts, _ = usually_sold_out_alert(conn, date, min_days, threshold)
    alert_html = "".join(
        f"<tr><td>{esc(shop)}</td><td>{esc(cast)}</td>"
        f"<td>{_badge(norm)}</td><td>{rate:.0%} ({n_sold}/{n}日)</td></tr>"
        for shop, cast, norm, raw, n_sold, n, rate in alerts)
    alert_html = (f"<table><tr><th>店舗</th><th>キャスト</th><th>今</th>"
                  f"<th>過去完売率</th></tr>{alert_html}</table>"
                  if alerts else '<p class="mut">該当なし</p>')

    fc = section_forecast(conn, date, min_days)
    fc_html = "".join(f"<div>{esc(l.strip())}</div>" for l in fc
                      if l.strip() and not l.startswith("  採点"))
    ev = [l for l in fc if "採点" in l or l.strip().startswith("202")]
    ev_html = "".join(f'<div class="mut">{esc(l.strip())}</div>'
                      for l in ev)

    watch_lines = section_watch(conn, date)
    watch_html = "".join(f"<div>{esc(l.strip())}</div>"
                         for l in watch_lines)

    move_lines = section_rank_moves(conn)
    move_html = "".join(f"<div>{esc(l.strip())}</div>" for l in move_lines)

    streak = section_streaks(conn, dates, 2)
    streak_html = "".join(f"<div>{esc(l.strip())}</div>" for l in streak)

    mom = section_momentum(conn, date)
    mom_html = "".join(f"<div>{esc(l.strip())}</div>" for l in mom)

    newc = section_new_casts(conn, date)
    newc_html = "".join(f"<div>{esc(l.strip())}</div>" for l in newc)

    # weekday x hour heatmap of first_sold_out_at (all history)
    heat = {}
    for wd, hh, n in conn.execute(
            """SELECT strftime('%w', target_date),
                      CAST(substr(first_sold_out_at, 12, 2) AS INTEGER),
                      COUNT(*)
               FROM daily_cast_status WHERE first_sold_out_at IS NOT NULL
               GROUP BY 1, 2"""):
        heat[(int(wd), hh)] = n
    hmax = max(heat.values()) if heat else 1
    wd_hdr = "".join(f"<th>{_WD[(i + 1) % 7]}</th>" for i in range(7))
    body_rows = ""
    for hh in range(6, 24):
        cells = f"<td class='mut'>{hh:02d}</td>"
        for py_wd in range(7):
            n = heat.get(((py_wd + 1) % 7, hh), 0)
            op = 0.12 + 0.85 * n / hmax if n else 0.03
            cells += (f'<td style="background:rgba(255,107,129,{op:.2f});'
                      f'text-align:center">{n or ""}</td>')
        body_rows += f"<tr>{cells}</tr>"
    for hh in range(0, 6):
        cells = f"<td class='mut'>{hh:02d}</td>"
        for py_wd in range(7):
            n = heat.get(((py_wd + 1) % 7, hh), 0)
            op = 0.12 + 0.85 * n / hmax if n else 0.03
            cells += (f'<td style="background:rgba(255,107,129,{op:.2f});'
                      f'text-align:center">{n or ""}</td>')
        body_rows += f"<tr>{cells}</tr>"
    heat_html = (f"<table><tr><th>時</th>{wd_hdr}</tr>"
                 f"{body_rows}</table>"
                 '<div class="mut">完売の初観測時刻の分布（全期間, 件数）</div>')

    cov_warn = []
    low_cov = _low_coverage_shops(conn, date)
    for s, h in sorted(low_cov.items(), key=lambda x: x[1]):
        cov_warn.append(
            f'<div class="warn">⚠ {esc(names.get(s, str(s)))}: '
            f'今日 {h}時間のみ取得（巡回欠損の可能性）</div>')
    cov_html = "".join(cov_warn) or '<p class="mut">異常なし</p>'

    gen = now_jst().strftime("%Y-%m-%d %H:%M JST")
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>estama tracker - {date}</title><style>{_DASH_CSS}</style></head>
<body>
<h1>estama tracker</h1>
<div class="sub">営業日 {date} / 生成 {gen} / R3最終状態</div>
<div class="cards">{cards}</div>
<section><h2>🔔 アラート: 普段完売なのに空き</h2>{alert_html}</section>
<section><h2>⭐ ウォッチ中キャスト</h2>{watch_html}</section>
<section><h2>完売（{counts.get('sold_out', 0)}人）</h2>{table('sold_out')}</section>
<section><h2>残りわずか（{counts.get('low', 0)}人）・完売予兆</h2>
{table('low')}<div class="mut" style="margin-top:8px">{fc_html}</div>
{ev_html}</section>
<section><h2>🚀 予約の勢い（枠消化）</h2>{mom_html}</section>
<section><h2>📈 ランキング変動（大阪全体・週間）</h2>{move_html}</section>
<section><h2>🆕 新規cast・移籍</h2>{newc_html}</section>
<section><h2>🗓 完売ヒートマップ（曜日×時刻）</h2>{heat_html}</section>
<section><h2>🔥 連続完売</h2>{streak_html}</section>
<section><h2>⚠ データカバレッジ警告</h2>{cov_html}</section>
<div class="mut" style="margin-top:8px">
estama-tracker auto-generated dashboard</div>
</body></html>"""


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
    p.add_argument("--date", help="business date (default: latest)")
    p.add_argument("--min-days", type=int, default=2)
    p.add_argument("--threshold", type=float, default=0.6)
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("dashboard",
                       help="write a self-contained HTML dashboard")
    p.add_argument("--date", help="business date (default: latest)")
    p.add_argument("--out", default=str(BASE / "dashboard.html"))
    p.add_argument("--db", default=str(BASE / "estama.db"))
    p = sub.add_parser("cast",
                       help="one cast's profile: attendance, sell-out "
                            "hours, recent days")
    p.add_argument("--shop-id", type=int, required=True)
    p.add_argument("--cast-id", type=int)
    p.add_argument("--cast-name")
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
    if args.cmd == "watch":
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
    if args.cmd == "notify":
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
    if args.cmd == "dashboard":
        html = build_dashboard(conn, args.date)
        Path(args.out).write_text(html, encoding="utf-8")
        print(f"dashboard written: {args.out}")
        return 0
    if args.cmd == "cast":
        if args.cast_id is None and args.cast_name is None:
            print("cast needs --cast-id or --cast-name")
            return 2
        for line in section_cast_detail(conn, args.shop_id,
                                        args.cast_id, args.cast_name):
            print(line)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
