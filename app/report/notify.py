"""Notify text payloads (alert + sell-out forecast summary).

Extracted call-for-call from report_sections.py; behaviour identical.
"""
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

from report.core import section_momentum, section_new_casts, section_rank_moves
from report.forecast import sellout_forecast

from common import (BASE_DIR as BASE, business_date, business_day_start,
                    load_json, now_jst)
from analysis import alert_evidence_lines, usually_sold_out_alert
from queries import (_all_dates, _ckey, _daily_dates, _day_finals,
                     _final_sql, _resolve_cast,
                     daily_finals, shop_areas, shop_names, status_counts)

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
