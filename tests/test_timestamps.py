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
