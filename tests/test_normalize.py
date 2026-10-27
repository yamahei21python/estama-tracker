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
    ("TELのみ", "dummy"),
    ("今すぐご案内 / TELのみ", "dummy"),
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

# --- TEL column (data-idx vertical mapping + pattern) ---
from bs4 import BeautifulSoup  # noqa: E402

TEL_HTML = """
<table>
<thead><tr>
  <th></th>
  <th class="th_sce"><div class="item-name"><h4>正常(20)</h4></div>
    <div class="item-date">10:00～18:00</div>
    <a class="link-detail" href="/shop/1/cast/111/"></a></th>
  <th class="th_sce"><div class="item-name"><h4>ダミー(22)</h4></div>
    <div class="item-date">12:00～26:00</div>
    <a class="link-detail" href="/shop/1/cast/222/"></a></th>
</tr></thead>
<tbody>
<tr><th>10:00</th>
  <td data-idx="111">○</td><td data-idx="222">TEL</td></tr>
<tr><th>10:30</th>
  <td data-idx="111">─</td><td data-idx="222">TEL</td></tr>
<tr><th>11:00</th>
  <td data-idx="111">○</td><td data-idx="222">TEL</td></tr>
</tbody></table>
"""
s2 = EstamaScraper.__new__(EstamaScraper)
casts, _ = s2._parse_schedule_html(TEL_HTML, "2026-10-27", 0)
by_name = {c["cast_name"]: c for c in casts}

ok_cast = by_name.get("正常")
check("TEL fixture has 正常", ok_cast is not None)
if ok_cast:
    check("正常 keeps ○ pattern", ok_cast["slot_pattern"].startswith("○2コマ"))
    check("正常 status low (2 circles)", ok_cast["status_norm"] == "low")
    check("正常 tel_cells=0", ok_cast["slots_summary"]["tel_cells"] == 0)

dummy = by_name.get("ダミー")
check("TEL fixture has ダミー", dummy is not None)
if dummy:
    check("ダミー pattern all TEL", dummy["slot_pattern"] == "全3コマ TEL (電話のみ)")
    check("ダミー status_raw TELのみ", dummy["status_raw"] == "TELのみ")
    check("ダミー status_norm dummy", dummy["status_norm"] == "dummy")
    check("ダミー tel_cells=3", dummy["slots_summary"]["tel_cells"] == 3)
    check("ダミー shift kept", dummy["shift"] == "12:00～26:00")
    # vertical data-idx: TEL must land on 222, not shift columns
    check("ダミー no false circles", dummy["slots_summary"]["open_circles"] == 0)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
