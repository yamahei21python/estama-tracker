"""Monitoring & coverage health.

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

MONITOR_FAIL_MIN = 2        # fails before a shop is listed
MONITOR_FAIL_RATE = 0.2     # ... or this share of its scrapes
MONITOR_EMPTY_MIN = 2       # zero-row scrapes before a shop is listed
MONITOR_SLOT_MIN_RATE = 0.5 # slot-capture share below which we warn
CHECKPOINTS_PER_DAY = 24    # hourly checkpoints from 06:00

from ._util import _low_coverage_shops

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
    if not dates:
        # Raw snapshots are gone (retention window shorter than the
        # rollup, or a manual delete). Coverage is snapshot-only, so
        # say so instead of crashing the whole report.
        lines.append("  snapshots が空のため coverage を算出できません")
        return lines
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
