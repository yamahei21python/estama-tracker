"""Characterization golden for EstamaScraper._parse_schedule_html.

The parser used to be 154 lines / 56 branches and had no test that ran
a whole HTML document through it, so a split had nothing to prove
identity against. This freezes its full output (every field of every
cast, plus the has_next_page flag) for fixtures that reach each branch:
name from h4 vs div.item-name, cast links with/without an id, missing
shift/status cells, vertical data-idx mapping, a trailing action cell,
all-TEL dummy columns, today vs future-day normalization, missing
table/thead/tbody and both next-page states.

Regenerate deliberately (review the diff first):
    ESTAMA_UPDATE_GOLDEN=1 python3 tests/test_parse_golden.py
Run: python3 tests/test_parse_golden.py
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GOLDEN = HERE / "golden" / "parse_schedule.txt"

sys.path.insert(0, str(ROOT / "app"))
from estama_scraper import EstamaScraper  # noqa: E402

RICH = """
<table>
<thead><tr>
  <th>時間</th>
  <th><h4>花子(24)</h4>
      <a class="link-detail" href="/shop/1/cast/12345/">詳細</a>
      <div class="item-date">12:00～20:00</div>
      <div class="item-t_now">予約受付中</div></th>
  <th><div class="item-name">すず</div>
      <a class="link-detail" href="/shop/1/"></a></th>
  <th><h4>あや</h4>
      <a class="cast" href="/shop/9/cast/333/"></a>
      <div class="item-date">19:00～翌5:00</div>
      <div class="status">本日完売</div></th>
  <th><h4>TEL子</h4>
      <a class="link-detail" href="/cast/444/"></a></th>
</tr></thead>
<tbody>
<tr><th>10:00</th>
  <td data-idx="12345">○</td><td>◯</td>
  <td data-idx="333">×</td><td data-idx="444">TEL</td>
  <td class="tel-action">TEL</td></tr>
<tr><th>10:30</th>
  <td data-idx="12345">─</td><td>─</td>
  <td data-idx="333">×</td><td data-idx="444">TEL</td></tr>
<tr><th>11:00</th>
  <td data-idx="12345">○</td><td>─</td>
  <td data-idx="333">×</td><td data-idx="444">TEL</td></tr>
<tr><th>11:30</th>
  <td data-idx="444">─</td><td>○</td>
  <td data-idx="12345">×</td><td data-idx="333">○</td></tr>
<tr></tr>
<tr><th>12:00</th></tr>
</tbody></table>
<ul><li class="li_next"><a class="btn-disable" href="#">次</a></li></ul>
"""

MISC = """
<table>
<thead><tr>
  <th>時間</th>
  <th><h4>テル美(20)</h4><a class="link-detail" href="/cast/555/"></a>
      <div class="item-t_now">ご予約受付中</div></th>
  <th><h4>×子</h4><a class="link-detail" href="/cast/666/"></a></th>
  <th><h4>閑子</h4><a class="link-detail" href="/cast/777/"></a></th>
</tr></thead>
<tbody>
<tr><th>18:00</th><td data-idx="555">TEL</td><td data-idx="666">×</td>
  <td data-idx="777">─</td></tr>
<tr><th>18:30</th><td data-idx="555">TEL</td><td data-idx="666">×</td>
  <td data-idx="777">─</td></tr>
</tbody></table>
"""

NEXT_ENABLED = """
<table><thead><tr><th>時間</th>
<th><h4>次子</h4><a class="link-detail" href="/cast/888/"></a></th>
</tr></thead>
<tbody><tr><th>10:00</th><td data-idx="888">○</td></tr></tbody></table>
<ul><li class="li_next"><a href="#">次へ</a></li></ul>
"""

NEXT_NO_ANCHOR = """
<table><thead><tr><th>時間</th>
<th><h4>孤子</h4><a class="link-detail" href="/cast/999/"></a></th>
</tr></thead>
<tbody><tr><th>10:00</th><td data-idx="999">○</td></tr></tbody></table>
<li class="li_next"></li>
"""

# a data-idx that matches no cast must fall back to column position
POSITIONAL_FALLBACK = """
<table><thead><tr><th>時間</th>
<th><h4>位置子</h4><a class="link-detail" href="/cast/11/"></a></th>
<th><h4>隣子</h4><a class="link-detail" href="/cast/22/"></a></th>
</tr></thead>
<tbody><tr><th>10:00</th><td data-idx="11">○</td>
  <td data-idx="999">○</td></tr></tbody></table>
"""

NO_TABLE = "<div>nothing here</div>"
NO_THEAD = "<table><tbody><tr><th>10:00</th></tr></tbody></table>"
NO_TBODY = "<table><thead><tr><th>時間</th></tr></thead></table>"

CASES = [
    ("rich today", RICH, "2026-10-30", 0),
    ("rich future", RICH, "2026-10-31", 1),
    ("misc today", MISC, "2026-10-30", 0),
    ("misc future", MISC, "2026-10-31", 1),
    ("next enabled", NEXT_ENABLED, "2026-10-30", 0),
    ("next li without anchor", NEXT_NO_ANCHOR, "2026-10-30", 0),
    ("unknown data-idx falls back to position", POSITIONAL_FALLBACK,
     "2026-10-30", 0),
    ("no table", NO_TABLE, "2026-10-30", 0),
    ("no thead", NO_THEAD, "2026-10-30", 0),
    ("no tbody", NO_TBODY, "2026-10-30", 0),
]

parser = EstamaScraper.__new__(EstamaScraper)
blocks = []
for label, html, date, offset in CASES:
    casts, has_next = parser._parse_schedule_html(html, date, offset)
    payload = {
        "case": label,
        "target_date": date,
        "days_offset": offset,
        "has_next_page": has_next,
        "casts": casts,
    }
    blocks.append(json.dumps(payload, ensure_ascii=False, indent=2,
                             sort_keys=True))
actual = "\n".join(blocks) + "\n"

update = os.environ.get("ESTAMA_UPDATE_GOLDEN") == "1"
state = None
if update or not GOLDEN.exists():
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(actual, encoding="utf-8")
    state = "updated" if update else "created"

fails = []
if state:
    print(f"{state}: {GOLDEN.relative_to(ROOT)}")
else:
    expected = GOLDEN.read_text(encoding="utf-8")
    if actual == expected:
        print("PASS golden identical: parse_schedule")
    else:
        fails.append("parse_schedule")
        exp = expected.splitlines()
        act = actual.splitlines()
        diff = next((f"  line {i + 1}:\n   expected {e!r}\n   actual   {a!r}"
                     for i, (e, a) in enumerate(zip(exp, act)) if e != a),
                    f"  line count {len(exp)} vs {len(act)}")
        print("FAIL golden identical: parse_schedule")
        print(diff)

# Anchors that make a silent golden regeneration of a broken parser
# still fail: the golden alone cannot tell "changed on purpose" apart.
casts, _ = parser._parse_schedule_html(RICH, "2026-10-30", 0)
by_name = {c["cast_name"]: c for c in casts}
checks = [
    ("names parsed", sorted(by_name) == ["TEL子", "あや", "すず", "花子"]),
    ("age from parens", by_name["花子"]["age"] == 24),
    ("no age without parens", by_name["あや"]["age"] is None),
    ("cast ids", [by_name[n]["cast_id"] for n in
                  ("花子", "すず", "あや", "TEL子")]
     == [12345, None, 333, 444]),
    ("vertical data-idx wins", by_name["あや"]["slots_summary"]
     == {"open_circles": 1, "reserved_crosses": 3,
         "off_dashes": 0, "tel_cells": 0}),
    ("trailing action cell ignored", by_name["TEL子"]["slots_summary"]
     == {"open_circles": 0, "reserved_crosses": 0,
         "off_dashes": 1, "tel_cells": 3}),
    ("shift parsed", by_name["花子"]["work_hours"] == 8.0),
    ("shift with 翌", by_name["あや"]["work_hours"] == 10.0),
    ("slot-estimated shift", by_name["すず"]["shift_end"] == "12:00"),
    ("future never sold_out",
     parser._parse_schedule_html(RICH, "2026-10-31", 1)[0][2]
     ["status_norm"] != "sold_out"),
]
for name, cond in checks:
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
