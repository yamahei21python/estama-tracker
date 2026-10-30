"""Report sections, forecasts, monitoring, notify and dashboard.

Every section returns printable lines (dashboard returns HTML); the
tracker only parses arguments and prints. This is the module that was
extracted verbatim from estama_tracker.py in stage 4 so the analysis
code could be characterized and split from the CLI.
"""
import html
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

from common import (BASE_DIR as BASE, business_date, business_day_start,
                    load_json, now_jst)
from analysis import alert_evidence_lines, usually_sold_out_alert
from queries import (_all_dates, _ckey, _day_finals, _final_sql,
                     _resolve_cast, shop_areas, shop_names, status_counts)

FLIP_HOUR = 6
SELLOUT_HORIZON_HOURS = 4
DEEP_SHOPS_JSON = BASE / "deep_shops.json"

MONITOR_FAIL_MIN = 2        # fails before a shop is listed
MONITOR_FAIL_RATE = 0.2     # ... or this share of its scrapes
MONITOR_EMPTY_MIN = 2       # zero-row scrapes before a shop is listed
MONITOR_SLOT_MIN_RATE = 0.5 # slot-capture share below which we warn
CHECKPOINTS_PER_DAY = 24    # hourly checkpoints from 06:00

_WD = "日月火水木金土"
_SLOT_RE = re.compile(r"○(\d+)コマ / ×(\d+)コマ")
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




def section_daily(conn, dates):
    """Per-day R3 final-state counts with sold-out rate."""
    lines = []
    expected = business_date(now_jst())
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
    """Casts sold out on >= streak_min consecutive days up to the newest.

    The streak must include the newest date; a cast that was sold out
    earlier but absent or different today is not listed (its run has
    ended as far as the newest day is concerned).
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
    lines.sort(key=lambda x: (-x[0], x[1] != "sold_out", str(x[3][0]),
                              str(x[2])))
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
           GROUP BY 1, 3, 4 ORDER BY 1, 3, 4""").fetchall()
    by_cast = {}
    for sid, name, k, d, fa in per_cast:
        by_cast.setdefault((sid, name, k), {})[d] = fa[11:13] if fa else "-"
    names = shop_names(conn)
    regulars = sorted(
        ((len(v), kk, v) for kk, v in by_cast.items() if len(v) >= 2),
        key=lambda x: (-x[0], str(x[1])))
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
    trans = Counter((prev[k], curr[k]) for k in sorted(both))
    for (x, y), n in trans.most_common(10):
        lines.append(f"    {x:10s} -> {y:10s}  {n:4d}")
    return lines


def section_area(conn, date, top_n=15):
    """Sold-out rate by shops.area for one date (R3 final)."""
    area = shop_areas(conn)
    agg = {}
    for r in _day_finals(conn, date):
        a = area.get(r["shop_id"], "(未設定)")
        s = agg.setdefault(a, {"so": 0, "cast": 0, "shops": set()})
        s["cast"] += 1
        s["shops"].add(r["shop_id"])
        if r["status_norm"] == "sold_out":
            s["so"] += 1
    ranked = sorted(agg.items(),
                    key=lambda x: (-x[1]["so"] / max(x[1]["cast"], 1), x[0]))
    return [
        f"  {a[:24]:24s} sold_out={s['so']:3d}/{s['cast']:4d} "
        f"({s['so'] / s['cast'] * 100:5.1f}%)  {len(s['shops'])}店"
        for a, s in ranked[:top_n]]


def section_shops(conn, date, top_n=15, min_casts=3):
    """Top shops by sold-out rate for one date (R3 final)."""
    name = shop_names(conn)
    agg = {}
    for r in _day_finals(conn, date):
        s = agg.setdefault(r["shop_id"], {"so": 0, "cast": 0})
        s["cast"] += 1
        if r["status_norm"] == "sold_out":
            s["so"] += 1
    ranked = sorted(
        ((sid, s) for sid, s in agg.items() if s["cast"] >= min_casts),
        key=lambda x: (-x[1]["so"] / x[1]["cast"], name.get(x[0], "")))
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
    """
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
    """{(shop_id, cast_key): [sold_days, observed_days]} before `date`."""
    hist = {}
    for d in _all_dates(conn):
        if d >= date:
            continue
        for r in _day_finals(conn, d):
            h = hist.setdefault((r["shop_id"], _ckey(r)), [0, 0])
            h[1] += 1
            h[0] += r["status_norm"] == "sold_out"
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
        ((prev[c][0] - now[c][0], c, now[c])
         for c in sorted(now.keys() & prev.keys())
         if abs(prev[c][0] - now[c][0]) >= min_delta),
        key=lambda x: (x[0], x[2][0], x[1]))
    # risers and fallers each get a share of the limit so a day of many
    # drops can't crowd every riser out of the report; a side's unused
    # slots flow to the other (two-pass: trim one, refill the other)
    ups = [m for m in moved if m[0] > 0]
    downs = [m for m in moved if m[0] < 0]
    ups = ups[:max(limit // 2, limit - len(downs))]
    downs = downs[:limit - len(ups)]
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
    names = shop_names(conn)
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
    names = shop_names(conn)
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
        names = shop_names(conn)
        lines.append(f"  {d} にほぼ取得できていない店:")
        for s, h in sorted(stale.items(), key=lambda x: (x[1], x[0])):
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


def _bd_cond(col):
    """scrape_log/run_log rows belonging to one BUSINESS day (flip 06:00).

    Timestamps are JST strings ('YYYY-MM-DD HH:MM:SS JST'), so the
    business date is the wall date shifted back six hours. Bound with a
    date parameter - never interpolated."""
    return ("date(datetime(substr(replace(" + col + ", ' JST', ''),"
            " 1, 19), '-6 hours')) = ?")


def _checkpoints_expected(date, now):
    start = business_day_start(date, now.tzinfo)
    if now < start:
        return 0
    if date < business_date(now):
        return CHECKPOINTS_PER_DAY
    return min(CHECKPOINTS_PER_DAY,
               int((now - start).total_seconds() // 3600) + 1)


def _monitor_checkpoints(conn, date, now, has_day):
    """(warnings, ok, expected, recorded_runs) from run_log."""
    warnings = []
    ck = {}
    total_ck = 0
    try:
        total_ck = conn.execute(
            "SELECT COUNT(*) FROM run_log WHERE job = 'checkpoint'"
        ).fetchone()[0]
        ck = dict(conn.execute(
            "SELECT status, COUNT(*) FROM run_log"
            f" WHERE job = 'checkpoint' AND {_bd_cond('started_at')}"
            " GROUP BY 1", (date,)).fetchall())
    except sqlite3.OperationalError:
        pass
    expected = _checkpoints_expected(date, now)
    ok = ck.get("ok", 0)
    if total_ck == 0:
        # snaps but no scheduler history: the DB was scraped by a runner
        # that does not write run_log (or the table was lost). Counts
        # would be a fabrication, so say what is actually known.
        if has_day:
            warnings.append("run_log未記録: checkpoint実績が0件"
                            "（runner未更新/未実行の疑い）")
    elif expected and (expected - ok >= 2 or ck.get("fail")):
        detail = f"ok {ok}/{expected}"
        for st in ("fail", "skip"):
            if ck.get(st):
                detail += f", {st} {ck[st]}"
        warnings.append(f"checkpoint未達: {detail}")
    if date == business_date(now) and not has_day:
        warnings.append("本日分のsnapshotsが0行（巡回失敗の疑い）")

    try:
        stale = conn.execute(
            "SELECT job, started_at FROM run_log WHERE status = 'running'"
            " AND started_at < ?",
            ((now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
             + " JST",)).fetchall()
    except sqlite3.OperationalError:
        stale = []
    for job, st in stale:
        warnings.append(f"runner滞留: {job} {st} (2時間超 running)")
    return warnings, ok, expected, total_ck


def _monitor_scrapes(conn, date, names):
    """(warnings, scrapes, fails, empty_shop_list) from scrape_log."""
    warnings = []
    try:
        rows = conn.execute(
            "SELECT shop_id, SUM(status = 'fail'),"
            f" SUM(status = 'empty'), COUNT(*) FROM scrape_log"
            f" WHERE {_bd_cond('fetched_at')} GROUP BY 1",
            (date,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    scrapes = sum(n for *_, n in rows)
    fails = sum(f for _, f, _, _ in rows)
    bad = [(s, f, n) for s, f, _, n in rows
           if f >= max(MONITOR_FAIL_MIN, round(n * MONITOR_FAIL_RATE))]
    if bad:
        bad.sort(key=lambda x: -x[1] / x[2])
        warnings.append("取得失敗の多い店: " + ", ".join(
            f"{names.get(s, s)} {f}/{n}" for s, f, n in bad[:5]))
    empty = [(s, e) for s, _, e, _ in rows if e >= MONITOR_EMPTY_MIN]
    if empty:
        empty.sort(key=lambda x: -x[1])
        warnings.append("0行店: " + ", ".join(
            f"{names.get(s, s)}({e}回)" for s, e in empty[:5]))
    return warnings, scrapes, fails, empty


def _monitor_slots(conn, date, now, names):
    """(warning or None, slot coverage text) from slot_events."""
    try:
        obs = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT shop_id,"
            " CAST(COALESCE(cast_id, cast_name) AS TEXT) ck FROM snapshots"
            " WHERE target_date = ?)", (date,)).fetchone()[0]
        cap = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT shop_id, cast_key"
            " FROM slot_events WHERE target_date = ?)",
            (date,)).fetchone()[0]
        has_events = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM slot_events)").fetchone()[0]
    except sqlite3.OperationalError:
        return None, "n/a"
    if not obs:
        return None, "n/a"
    slot_txt = f"{cap}/{obs} cast ({cap / obs:.0%})"
    if has_events and cap / obs < MONITOR_SLOT_MIN_RATE:
        missing = conn.execute(
            """SELECT shop_id, COUNT(DISTINCT CAST(COALESCE(cast_id,
                   cast_name) AS TEXT)) FROM snapshots
               WHERE target_date = ?
                 AND (shop_id, CAST(COALESCE(cast_id, cast_name)
                      AS TEXT)) NOT IN
                     (SELECT shop_id, cast_key FROM slot_events
                      WHERE target_date = ?)
               GROUP BY 1 ORDER BY 2 DESC LIMIT 5""",
            (date, date)).fetchall()
        return (f"枠イベント取得率が低い: {slot_txt}"
                + (" 未取得: " + ", ".join(
                    f"{names.get(s, s)}({n})" for s, n in missing)
                   if missing else "")), slot_txt
    if not has_events and date == business_date(now):
        return "枠イベントが1件も記録されていない（取得失敗の疑い）", slot_txt
    return None, slot_txt


def monitor_report(conn, date, now=None):
    """{'summary': str, 'warnings': [str]} for one business day.

    Checkpoint counts come from run_log (the scheduler's view), shop
    health from scrape_log (the scraper's view) and slot capture from
    slot_events vs snapshots. Missing tables (old DBs, plain copies)
    degrade to 'n/a' instead of failing the report."""
    now = now or now_jst()
    names = shop_names(conn)
    has_day = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM snapshots WHERE target_date = ?)",
        (date,)).fetchone()[0]

    warnings, ok, expected, recorded = _monitor_checkpoints(
        conn, date, now, has_day)
    w, scrapes, fails, empty = _monitor_scrapes(conn, date, names)
    warnings += w
    slot_warn, slot_txt = _monitor_slots(conn, date, now, names)
    if slot_warn:
        warnings.append(slot_warn)

    parts = [f"checkpoint ok {ok}/{expected}" if expected and recorded
             else "checkpoint n/a"]
    if scrapes:
        parts.append(f"取得fail {fails}/{scrapes}")
    parts.append(f"枠イベント {slot_txt}")
    if empty:
        parts.append(f"0行店 {len(empty)}")
    return {"summary": " / ".join(parts), "warnings": warnings}


def section_monitor(conn, date, now=None):
    """Render monitor_report: one summary line + '!' warning lines."""
    rep = monitor_report(conn, date, now)
    lines = ["  " + rep["summary"]]
    lines += [f"  ! {w}" for w in rep["warnings"]]
    return lines


def build_notify_text(conn, date, min_days=2, threshold=0.6, limit=8):
    """Notification body: alerts + forecast + rank moves + watch hits.

    Short by design - the report/dashboard carry the detail; the push
    only needs 'what changed that I should open the site for'."""
    parts = [f"[estama] {date} レポート"]
    alerts, _ = usually_sold_out_alert(conn, date, min_days, threshold)
    if alerts:
        parts.append("■ 普段完売なのに空き:")
        for i, (shop, cast, norm, raw, n_sold, n, rate) in enumerate(
                alerts[:limit]):
            parts.append(f"  {shop} - {cast} [{raw}] (完売率 {rate:.0%})")
            if i < 3:
                parts += alert_evidence_lines(conn, shop, cast, date,
                                              prefix="    ", compact=True)
    try:
        fc = sellout_forecast(conn, date)
    except Exception:
        fc = None
    if fc and fc["qualified"]:
        parts.append("■ 4時間以内に完売しそう:")
        for c in fc["qualified"]:
            parts.append(
                f"  p={c['p']:.0%} {c['shop']} - {c['cast_name']}"
                f" (完売率 {c['rate']:.0%} {c['hist_sold']}/{c['hist_days']}日)")
    elif fc and fc["baseline"]:
        parts.append("■ 4時間以内に完売: 履歴不足のため確率なし"
                     " / ベースライン上位: "
                     + ", ".join(c["cast_name"] for c in fc["baseline"]))
    try:
        watch = conn.execute(
            """SELECT w.shop_id, w.cast_key, w.cast_name FROM cast_watch w
               ORDER BY w.added_at, w.shop_id, w.cast_key""").fetchall()
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
        # cap mover lines but always keep the IN/OUT summary lines -
        # they sit at the tail of the section and a flat [:8] cut
        # would silently drop them on busy days
        movers = [l for l in moves if "↑" in l or "↓" in l]
        keep = movers[:8] + [l for l in moves
                             if "新規IN" in l or "圏外OUT" in l]
        parts += keep
        if len(movers) > 8:
            parts.append(f"  ...他 {len(movers) - 8}件の変動")
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


def _badge(norm):
    label = {"sold_out": "完売", "low": "残少", "available": "空き",
             "closed": "終了", "dummy": "ダミー", "unknown": "不明"
             }.get(norm, norm)
    cls = {"sold_out": "b-sold", "low": "b-low", "available": "b-avail",
           "closed": "b-closed"}.get(norm, "b-unknown")
    return f'<span class="badge {cls}">{label}</span>'


def _dash_table(finals, names, status):
    """One status table of R3 finals, sorted by shop then cast."""
    esc = html.escape
    rs = [r for r in finals if r["status_norm"] == status]
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


def _dash_alerts(conn, date, min_days, threshold):
    """Alert table HTML (usually sold out, available/low today)."""
    esc = html.escape
    alerts, _ = usually_sold_out_alert(conn, date, min_days, threshold)
    body = "".join(
        f"<tr><td>{esc(shop)}</td><td>{esc(cast)}</td>"
        f"<td>{_badge(norm)}</td><td>{rate:.0%} ({n_sold}/{n}日)</td></tr>"
        for shop, cast, norm, raw, n_sold, n, rate in alerts)
    return (f"<table><tr><th>店舗</th><th>キャスト</th><th>今</th>"
            f"<th>過去完売率</th></tr>{body}</table>"
            if alerts else '<p class="mut">該当なし</p>')


def _dash_forecast(conn, date, min_days):
    """(forecast lines HTML, self-score HTML)."""
    esc = html.escape
    fc = section_forecast(conn, date, min_days)
    fc_html = "".join(f"<div>{esc(l.strip())}</div>" for l in fc
                      if l.strip() and not l.startswith("  採点"))
    ev = [l for l in fc if "採点" in l or l.strip().startswith("202")]
    ev_html = "".join(f'<div class="mut">{esc(l.strip())}</div>'
                      for l in ev)
    return fc_html, ev_html


def _dash_lines(lines):
    """Plain lines (already formatted by a section) -> HTML divs."""
    esc = html.escape
    return "".join(f"<div>{esc(l.strip())}</div>" for l in lines)


def _dash_heat(conn):
    """Weekday x hour heatmap of first_sold_out_at (all history)."""
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
    for hh in list(range(6, 24)) + list(range(0, 6)):
        cells = f"<td class='mut'>{hh:02d}</td>"
        for py_wd in range(7):
            n = heat.get(((py_wd + 1) % 7, hh), 0)
            op = 0.12 + 0.85 * n / hmax if n else 0.03
            cells += (f'<td style="background:rgba(255,107,129,{op:.2f});'
                      f'text-align:center">{n or ""}</td>')
        body_rows += f"<tr>{cells}</tr>"
    return (f"<table><tr><th>時</th>{wd_hdr}</tr>"
            f"{body_rows}</table>"
            '<div class="mut">完売の初観測時刻の分布（全期間, 件数）</div>')


def _dash_coverage(conn, date, names):
    """Low-coverage warnings HTML."""
    esc = html.escape
    low_cov = _low_coverage_shops(conn, date)
    cov_warn = [
        f'<div class="warn">⚠ {esc(names.get(s, str(s)))}: '
        f'今日 {h}時間のみ取得（巡回欠損の可能性）</div>'
        for s, h in sorted(low_cov.items(), key=lambda x: (x[1], x[0]))]
    return "".join(cov_warn) or '<p class="mut">異常なし</p>'


def build_dashboard(conn, date=None, min_days=2, threshold=0.6):
    """Self-contained HTML dashboard. No JS, no external assets."""
    dates = _all_dates(conn)
    date = date or (dates[-1] if dates else now_jst().date().isoformat())
    counts = dict(status_counts(conn, date))
    finals = _day_finals(conn, date)
    names = shop_names(conn)

    cards = "".join(
        f'<div class="card c-{k}"><div class="n">{counts.get(k, 0)}</div>'
        f'<div class="l">{label}</div></div>'
        for k, label in [("sold_out", "完売"), ("low", "残りわずか"),
                         ("available", "空き"), ("closed", "受付終了"),
                         ("dummy", "ダミー"), ("unknown", "不明")])
    alert_html = _dash_alerts(conn, date, min_days, threshold)
    fc_html, ev_html = _dash_forecast(conn, date, min_days)
    watch_html = _dash_lines(section_watch(conn, date))
    move_html = _dash_lines(section_rank_moves(conn))
    streak_html = _dash_lines(section_streaks(conn, dates, 2))
    mom_html = _dash_lines(section_momentum(conn, date))
    newc_html = _dash_lines(section_new_casts(conn, date))
    heat_html = _dash_heat(conn)
    cov_html = _dash_coverage(conn, date, names)

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
<section><h2>完売（{counts.get('sold_out', 0)}人）</h2>{_dash_table(finals, names, 'sold_out')}</section>
<section><h2>残りわずか（{counts.get('low', 0)}人）・完売予兆</h2>
{_dash_table(finals, names, 'low')}<div class="mut" style="margin-top:8px">{fc_html}</div>
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
