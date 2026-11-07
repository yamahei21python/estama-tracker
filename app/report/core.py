"""Core report sections.

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

from ._util import _WD, _SLOT_RE, _low_coverage_shops

def section_daily(conn, dates):
    """Per-day R3 final-state counts with sold-out rate.

    Reads the rollup so the summary keeps covering the full history
    after raw snapshots are pruned; falls back to the snapshot window
    for days that were never rolled up."""
    lines = []
    expected = business_date(now_jst())
    for d in dates:
        c = Counter(r[3] for r in daily_finals(conn, d))
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
        for shop_id, cast_name, cast_id, status_norm in daily_finals(conn, d):
            k = (shop_id, str(cast_id) if cast_id is not None else cast_name)
            by_cast.setdefault(k, {})[d] = status_norm
            names[k] = (cast_name, cast_id)
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
    # first_seen must come from the rollup: snapshots only cover the
    # retention window, and a 15-day history would flag every long-time
    # cast as "new" (measured: 199 vs the correct 162 on 2026-11-05).
    try:
        first_seen = dict(conn.execute(
            """SELECT cast_key, MIN(target_date)
               FROM daily_cast_status GROUP BY 1"""))
    except sqlite3.OperationalError:
        first_seen = {}
    if not first_seen:
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
