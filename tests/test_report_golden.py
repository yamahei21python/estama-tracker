"""Golden outputs for report / dashboard / notify (stage-4 condition).

The tracker split is only safe if the three user-visible outputs stay
byte-identical. This test freezes them on the deterministic fixture DB
(tests/_fixture.py) with the clock pinned, so the comparison is stable
on any machine and any day.

Regenerate deliberately (review the diff first):
    ESTAMA_UPDATE_GOLDEN=1 python3 tests/test_report_golden.py
Run: python3 tests/test_report_golden.py
"""
import atexit
import io
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GOLDEN = HERE / "golden"
SCRATCH = Path(tempfile.mkdtemp(prefix="estama-golden-test-"))
atexit.register(shutil.rmtree, SCRATCH, True)

os.environ["ESTAMA_REVIEW_DB"] = str(SCRATCH / "no-review.db")
os.environ["ESTAMA_BAKUSAI_DB"] = str(SCRATCH / "no-bakusai.db")

sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(HERE))

import _fixture  # noqa: E402
import analysis  # noqa: E402
import estama_tracker as t  # noqa: E402
import report_sections  # noqa: E402
from common import connect_db  # noqa: E402

D3 = _fixture.DATES[-1]
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
# each module imported now_jst by name; pin all three so the clock is
# a non-factor both before and after the stage-4 split
t.now_jst = lambda: NOW
analysis.now_jst = lambda: NOW
report_sections.now_jst = lambda: NOW

fails = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)
        if detail:
            print(detail)


conn = connect_db(_fixture.build(SCRATCH / "estama.db"))
t.rollup_daily(conn)

buf = io.StringIO()
with redirect_stdout(buf):
    t.print_report(conn, D3, evidence=True)
outputs = {
    "report.txt": buf.getvalue(),
    "dashboard.html": t.build_dashboard(conn, D3),
    "notify.txt": t.build_notify_text(conn, D3) + "\n",
}

GOLDEN.mkdir(exist_ok=True)
update = os.environ.get("ESTAMA_UPDATE_GOLDEN") == "1"
for name, actual in outputs.items():
    path = GOLDEN / name
    if update or not path.exists():
        path.write_text(actual, encoding="utf-8")
        state = "updated" if update else "created"
        print(f"{state}: {path.relative_to(ROOT)}")
        continue
    expected = path.read_text(encoding="utf-8")
    if actual == expected:
        check(f"golden identical: {name}", True)
        continue
    exp_lines = expected.splitlines()
    act_lines = actual.splitlines()
    diff = next((f"  line {i + 1}:\n   expected {e!r}\n   actual   {a!r}"
                 for i, (e, a) in enumerate(zip(exp_lines, act_lines))
                 if e != a), f"  line count {len(exp_lines)} vs {len(act_lines)}")
    check(f"golden identical: {name}", False, diff)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
