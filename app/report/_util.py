"""Private helpers shared between report modules.

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

import re

_WD = "日月火水木金土"
_SLOT_RE = re.compile(r"○(\d+)コマ / ×(\d+)コマ")

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
