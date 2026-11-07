"""Regression tests for JST timestamps.

All stored timestamps (fetched_at / fetched_time / fetched_date) must be
Asia/Tokyo. Until 2026-10-26 the scrapers used naive datetime.now(); on
the UTC VM that stored UTC values under a 'JST' label, and a naive
fetched_date could be a day behind at the 06:15 JST ranking run.

Run: python3 tests/test_timestamps.py
"""
import re
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import common  # noqa: E402

passed = 0


def check(name, cond):
    global passed
    assert cond, f"FAILED: {name}"
    passed += 1
    print(f"ok: {name}")


now = common.now_jst()
check("now_jst is JST (+09:00)", now.utcoffset() == timedelta(hours=9))
check("now_jst tz name", now.tzname() == "JST")
check("fetched_at format", re.fullmatch(
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} JST",
    now.strftime("%Y-%m-%d %H:%M:%S JST")) is not None)

# No naive datetime.now() call may remain in the app code (AST scan, so
# docstrings and comments mentioning it do not count).
import ast


def naive_now_calls(path):
    tree = ast.parse(path.read_text())
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "now"
                and isinstance(func.value, ast.Name)
                and func.value.id == "datetime" and not node.args):
            hits.append(node.lineno)
    return hits


for f in ("estama_scraper.py", "ranking_scraper.py", "estama_tracker.py",
          "common.py"):
    check(f"no naive datetime.now() in {f}",
          naive_now_calls(ROOT / "app" / f) == [])

print(f"ALL {passed} PASSED")


# --- parse_timestamp/stamp (the single source for JST parsing) --------
import sys as _sys  # noqa: E402
from pathlib import Path as _P  # noqa: E402

_sys.path.insert(0, str(_P(__file__).resolve().parent.parent))

import common  # noqa: E402
import runner  # noqa: E402

check("run_log stamp parses back to the same wall clock",
      common.parse_timestamp(runner._run_ts()).replace(tzinfo=None) is not None)
check("JST-suffixed stamp round-trips",
      common.parse_timestamp("2026-11-05 23:05:00 JST")
      == __import__("datetime").datetime(2026, 11, 5, 23, 5))
check("ISO with offset converts to JST",
      common.parse_timestamp("2026-11-05T14:05:00+00:00")
      == __import__("datetime").datetime(2026, 11, 5, 23, 5))
check("ISO with JST offset is the same instant",
      common.parse_timestamp("2026-11-05T23:05:00+09:00")
      == __import__("datetime").datetime(2026, 11, 5, 23, 5))
check("naive values are taken as JST",
      common.parse_timestamp("2026-11-05T00:00:00")
      == __import__("datetime").datetime(2026, 11, 5, 0, 0))
check("garbage parses to None",
      common.parse_timestamp("not a date") is None
      and common.parse_timestamp(None) is None)
check("stamp() carries the JST suffix",
      common.stamp().endswith(" JST") and runner._run_ts().endswith(" JST"))
check("runner's shim delegates to the common parser",
      runner._as_jst_naive("2026-11-05 00:00:00 JST")
      == common.parse_timestamp("2026-11-05 00:00:00 JST"))
