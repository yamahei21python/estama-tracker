"""Sell-out forecasting (hazard model + candidates).

Extracted call-for-call from report_sections.py; behaviour identical.
"""
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

from common import (BASE_DIR as BASE, business_date, business_day_start,
                    load_json, now_jst)
from analysis import alert_evidence_lines, usually_sold_out_alert
from queries import (_all_dates, _ckey, _daily_dates, _day_finals,
                     _final_sql, _resolve_cast,
                     daily_finals, shop_areas, shop_names, status_counts)

import re

FLIP_HOUR = 6
SELLOUT_HORIZON_HOURS = 4
DEEP_SHOPS_JSON = BASE / "deep_shops.json"

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
    cands.sort(key=lambda x: (-x[0], -x[1], x[3]["cast_name"],
                              x[3]["shop_id"]))
    names = shop_names(conn)
    for rate, so, n, r in cands[:limit]:
        lines.append(
            f"  予兆 {names.get(r['shop_id'], r['shop_id'])}"
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


def load_deep_shop_ids(path=DEEP_SHOPS_JSON):
    """shop_ids from deep_shops.json (missing file -> empty set)."""
    data = load_json(path, default=None)
    if not data:
        return set()
    shops = data.get("shops", []) if isinstance(data, dict) else data
    ids = set()
    for s in shops:
        if isinstance(s, dict) and s.get("shop_id") is not None:
            ids.add(int(s["shop_id"]))
        elif isinstance(s, int):
            ids.add(s)
    return ids


def _bd_hour(hhmm):
    """Clock 'HH:MM' -> business-day hour (06:00->6 ... 05:00->29)."""
    if not hhmm:
        return None
    parts = str(hhmm).split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 29 and 0 <= m <= 59):
        return None
    if h < FLIP_HOUR:
        h += 24
    return h


def pooled_sellout_hazard(conn, date):
    """Pooled sell-out events / at-risk cast-days per business hour.

    A cast-day is at risk in bucket b when it was observed somewhere
    in [b, last observation] and had not sold out before b; the event
    lands in the bucket where its first sold_out observation falls.
    Returns (events: Counter, at_risk: Counter).

    Trains on daily_cast_status (one row per cast-day, kept a year)
    using the first_seen_at/last_seen_at envelope; falls back to raw
    snapshots for DBs that predate those columns or have no rollup.
    """
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(daily_cast_status)")}
    if {"first_seen_at", "last_seen_at"} <= cols:
        rows = conn.execute(
            """SELECT first_seen_at, last_seen_at, first_sold_out_at
               FROM daily_cast_status
               WHERE target_date < ?
                 AND first_seen_at IS NOT NULL
                 AND last_seen_at IS NOT NULL""", (date,)).fetchall()
        if rows:
            events, at_risk = Counter(), Counter()
            for first_at, last_at, so_at in rows:
                lo = _bd_hour(first_at[11:16])
                hi = _bd_hour(last_at[11:16])
                if lo is None or hi is None or hi < lo:
                    continue
                so = _bd_hour(so_at[11:16]) if so_at else None
                for b in range(lo, hi + 1):
                    if so is None or so >= b:
                        at_risk[b] += 1
                if so is not None:
                    events[so] += 1
            return events, at_risk

    rows = conn.execute(
        """SELECT shop_id, CAST(COALESCE(cast_id, cast_name) AS TEXT),
                  target_date, fetched_time, status_norm
           FROM snapshots WHERE target_date < ?""",
        (date,)).fetchall()
    days = {}
    for sid, ck, d, ft, norm in rows:
        b = _bd_hour(ft)
        if b is None:
            continue
        rec = days.setdefault((sid, ck, d), {"min": b, "max": b, "so": None})
        rec["min"] = min(rec["min"], b)
        rec["max"] = max(rec["max"], b)
        if norm == "sold_out" and (rec["so"] is None or b < rec["so"]):
            rec["so"] = b
    events, at_risk = Counter(), Counter()
    for rec in days.values():
        so = rec["so"]
        for b in range(rec["min"], rec["max"] + 1):
            if so is None or so >= b:
                at_risk[b] += 1
        if so is not None:
            events[so] += 1
    return events, at_risk


def _forecast_watch(conn):
    """{(shop_id, cast_key)} in cast_watch (empty on old DBs)."""
    try:
        return {(r[0], r[1]) for r in conn.execute(
            "SELECT shop_id, cast_key FROM cast_watch")}
    except sqlite3.OperationalError:
        return set()


def _forecast_history(conn, date):
    """{(shop_id, cast_key): [sold_days, observed_days]} before `date`.

    Uses the rollup so the forecast keeps a full year of history after
    raw snapshots are pruned (only ~2 weeks are retained)."""
    hist = {}
    for d in _daily_dates(conn):
        if d >= date:
            continue
        for shop_id, cast_name, cast_id, status_norm in daily_finals(conn, d):
            key = (shop_id, str(cast_id) if cast_id is not None else cast_name)
            h = hist.setdefault(key, [0, 0])
            h[1] += 1
            h[0] += status_norm == "sold_out"
    return hist


def _forecast_anchor(conn, date, at_hhmm):
    """Business hour the forecast starts from (explicit, now, or last)."""
    now = now_jst()
    anchor = None
    if at_hhmm:
        anchor = _bd_hour(at_hhmm)
    elif date == business_date(now):
        anchor = _bd_hour(now.strftime("%H:%M"))
    if anchor is None:
        times = [_bd_hour(t) for (t,) in conn.execute(
            "SELECT DISTINCT fetched_time FROM snapshots WHERE target_date = ?",
            (date,))]
        times = [t for t in times if t is not None]
        anchor = max(times) if times else 12
    return anchor


def _forecast_hazard(conn, date, anchor, horizon, min_obs):
    """(p4, overall hazard, observations at the anchor) from pooled data."""
    events, at_risk = pooled_sellout_hazard(conn, date)
    tot_ev, tot_risk = sum(events.values()), sum(at_risk.values())
    overall = min(tot_ev / tot_risk, 0.95) if tot_risk else 0.0
    n_now = at_risk.get(anchor, 0)
    p4 = 1.0
    for b in range(anchor, min(anchor + horizon, 30)):
        a, e = at_risk.get(b, 0), events.get(b, 0)
        h = min(e / a, 0.95) if a >= min_obs else overall
        p4 *= (1.0 - h)
    return 1.0 - p4, overall, n_now


def _forecast_candidates(conn, date, watch, deep_shop_ids, hist, n_now,
                         p4, min_obs):
    """Today's available/low watch/deep casts with probability fields."""
    names = shop_names(conn)
    cands = []
    for r in _day_finals(conn, date):
        if r["status_norm"] not in ("available", "low"):
            continue
        key = (r["shop_id"], _ckey(r))
        in_watch, in_deep = key in watch, r["shop_id"] in deep_shop_ids
        if not (in_watch or in_deep):
            continue
        so, nd = hist.get(key, [0, 0])
        rate = so / nd if nd else None
        p = None
        if nd and n_now >= min_obs:
            p = round(p4 * (rate ** 0.5), 3)
        cands.append({
            "shop_id": r["shop_id"],
            "shop": names.get(r["shop_id"], str(r["shop_id"])),
            "cast_key": key[1],
            "cast_name": r["cast_name"],
            "status_norm": r["status_norm"],
            "status_raw": r["status_raw"],
            "rate": rate,
            "hist_days": nd,
            "hist_sold": so,
            "p": p,
            "n": n_now,
            "tier": "watch" if in_watch else "deep",
        })
    return cands


def sellout_forecast(conn, date, at_hhmm=None,
                     horizon=SELLOUT_HORIZON_HOURS, top=5,
                     deep_shop_ids=None, min_obs=3, min_p=0.5):
    """P(sold_out within `horizon` hours) for watch/deep candidates.

    Candidates are today's available/low casts in cast_watch or in a
    deep-tier shop. Each candidate carries the pool hazard prob, its
    own baseline (sold-out days / observed days) and the observation
    count n behind the probability. n<min_obs or no cast history ->
    p is None and the cast goes to the baseline-only list.
    """
    if deep_shop_ids is None:
        deep_shop_ids = load_deep_shop_ids()
    watch = _forecast_watch(conn)
    hist = _forecast_history(conn, date)
    anchor = _forecast_anchor(conn, date, at_hhmm)
    p4, overall, n_now = _forecast_hazard(
        conn, date, anchor, horizon, min_obs)
    cands = _forecast_candidates(
        conn, date, watch, deep_shop_ids, hist, n_now, p4, min_obs)
    ranked = sorted([c for c in cands if c["p"] is not None],
                    key=lambda c: (-c["p"], -(c["rate"] or 0)))
    qualified = [c for c in ranked if c["p"] >= min_p][:top]
    baseline_only = sorted([c for c in cands if c["p"] is None],
                           key=lambda c: (-(c["rate"] or 0),
                                          -c["hist_sold"], c["cast_name"]))
    return {
        "anchor_hour": anchor,
        "horizon": horizon,
        "n": n_now,
        "pooled_hazard": round(p4, 3),
        "overall_hazard": round(overall, 3),
        "candidates": len(cands),
        "qualified": qualified,
        "baseline": baseline_only[:top],
    }


def section_sellout_forecast(conn, date, at_hhmm=None, horizon=4, top=5,
                             deep_shop_ids=None):
    """Report lines for the 4h sell-out forecast."""
    fc = sellout_forecast(conn, date, at_hhmm=at_hhmm, horizon=horizon,
                          top=top, deep_shop_ids=deep_shop_ids)
    lines = [f"  起点 {fc['anchor_hour']}:00 / {fc['horizon']}時間以内"
             f" / 対象cast {fc['candidates']}名"
             f" / 同時刻の履歴観測 n={fc['n']}"]
    if fc["n"] >= 3:
        lines[0] += (f" (pooled p={fc['pooled_hazard']:.0%},"
                     f" 全体hazard={fc['overall_hazard']:.0%})")
    else:
        lines[0] += " (n<3: 確率は出さない)"
    if not fc["candidates"]:
        lines.append("  対象なし（cast_watch 登録 or deep_shops.json の店舗が対象）")
        return lines
    if fc["qualified"]:
        for c in fc["qualified"]:
            lines.append(
                f"  予測 {c['p']:.0%} {c['shop']} - {c['cast_name']}:"
                f" [{c['status_raw']}] (完売率 {c['rate']:.0%}"
                f" {c['hist_sold']}/{c['hist_days']}日, n={c['n']},"
                f" {c['tier']})")
    else:
        lines.append(f"  確率 p>={0.5:.0%} の該当なし"
                     + (f"（履歴観測 n={fc['n']}<3 のため確率は出さない）"
                        if fc["n"] < 3 else ""))
    if fc["baseline"]:
        lines.append("  ベースライン順位（確率なし）:")
        for c in fc["baseline"]:
            rate = f"{c['rate']:.0%} {c['hist_sold']}/{c['hist_days']}日" \
                if c["rate"] is not None else "履歴なし"
            lines.append(f"    {c['shop']} - {c['cast_name']}:"
                         f" [{c['status_raw']}] 完売率 {rate} ({c['tier']})")
    return lines
