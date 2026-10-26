"""Equivalence tests for status normalization after the common.py
refactor: the text mapping (common.normalize_status_text) and the
scraper's slot-aware wrapper must reproduce the pre-refactor truth
table exactly, including the rule that future days never get sold_out.

Run: python3 tests/test_normalize.py  (needs requests/bs4 for the
scraper import; use the project venv)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from common import normalize_status_text  # noqa: E402
from estama_scraper import EstamaScraper  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


text_cases = [
    ("ご好評につき本日完売", "sold_out"),
    ("本日完売", "sold_out"),
    ("受付終了", "closed"),
    ("本日受付終了", "closed"),
    ("残りわずか", "low"),
    ("今すぐご案内", "available"),
    ("ご予約受付中", "available"),
    ("30分以内でご案内", "available"),
    ("時間のみ", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
]
for raw, want in text_cases:
    check(f"text {raw!r} -> {want}", normalize_status_text(raw) == want)

s = EstamaScraper.__new__(EstamaScraper)  # bypass __init__ (no network)
norm = s._normalize_status
slot_cases = [
    # (raw, circles, crosses, offset, want)
    ("時間のみ", 3, 0, 0, "available"),   # unmapped text falls through to slots
    ("時間のみ", 2, 0, 0, "low"),
    ("時間のみ", 0, 0, 0, "closed"),
    ("時間のみ", 0, 5, 0, "unknown"),
    ("ご好評につき本日完売", 0, 9, 1, "unknown"),  # future day: NEVER sold_out
    (None, 3, 0, 1, "available"),
    (None, 0, 4, 1, "unknown"),
    (None, 0, 0, 1, "closed"),
]
for raw, c, x, off, want in slot_cases:
    got = norm(raw, c, x, off)
    check(f"slots raw={raw!r} o={c} x={x} off={off} -> {want}", got == want)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
