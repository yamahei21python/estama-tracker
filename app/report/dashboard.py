"""HTML dashboard rendering.

Extracted call-for-call from report_sections.py; behaviour identical.
"""
from collections import Counter
from datetime import datetime, timedelta

from common import (BASE_DIR as BASE, business_date, business_day_start,
                    load_json, now_jst)
from analysis import alert_evidence_lines, usually_sold_out_alert
from queries import (_all_dates, _ckey, _daily_dates, _day_finals,
                     _final_sql, _resolve_cast,
                     daily_finals, shop_areas, shop_names, status_counts)

import html

from ._util import _WD, _low_coverage_shops

from report.core import (section_momentum, section_new_casts, section_rank_moves,
                         section_streaks, section_watch)
from report.forecast import section_forecast

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
