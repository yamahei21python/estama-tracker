"""Report sections, forecasts, monitoring, notify and dashboard.

Backward-compatible import shim: the implementation moved to the
`app/report/` package in 2026-11 (#22 PR-F). Importing from here keeps
estama_tracker.py, scripts and the tests working unchanged.
"""
# common/query/analysis bindings (tests pin `now_jst` here)
from common import (BASE_DIR as BASE, business_date, business_day_start,
                    load_json, now_jst)  # noqa: F401
from analysis import alert_evidence_lines, usually_sold_out_alert  # noqa: F401
from queries import (_all_dates, _ckey, _daily_dates, _day_finals, _final_sql,
                     _resolve_cast, daily_finals, shop_areas,
                     shop_names, status_counts)  # noqa: F401

from report._util import _WD, _SLOT_RE, _low_coverage_shops  # noqa: F401
from report.core import (  # noqa: F401
    section_daily, section_streaks, section_timing, section_transitions,
    section_area, section_shops, section_ranking, section_consistency,
    section_rank_moves, section_watch, _slot_counts, section_momentum,
    section_new_casts, section_cast_detail,
)
from report.forecast import (  # noqa: F401
    FLIP_HOUR, SELLOUT_HORIZON_HOURS, DEEP_SHOPS_JSON, load_deep_shop_ids,
    _bd_hour, pooled_sellout_hazard, _forecast_watch, _forecast_history,
    _forecast_anchor, _forecast_hazard, _forecast_candidates,
    sellout_forecast, section_forecast, section_sellout_forecast,
)
from report.health import (  # noqa: F401
    MONITOR_FAIL_MIN, MONITOR_FAIL_RATE, MONITOR_EMPTY_MIN,
    MONITOR_SLOT_MIN_RATE, CHECKPOINTS_PER_DAY, section_coverage,
    section_runlog, _bd_cond, _checkpoints_expected, _monitor_checkpoints,
    _monitor_scrapes, _monitor_slots, monitor_report, section_monitor,
)
from report.notify import build_notify_text  # noqa: F401
from report.dashboard import (  # noqa: F401
    _DASH_CSS, _badge, _dash_table, _dash_alerts, _dash_forecast,
    _dash_lines, _dash_heat, _dash_coverage, build_dashboard,
)
