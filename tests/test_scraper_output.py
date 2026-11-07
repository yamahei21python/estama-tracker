"""Tests for runner.run_scraper's output handling.

A failed hour used to leave only the last 3 lines of scraper output in
the cron log, which made the failure undebuggable after the fact. Now
the full output always goes to LOG_DIR and the tail length on failure
is 40 lines.

Run: python3 tests/test_scraper_output.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="estama-scraper-out-test-"))
os.environ["ESTAMA_VM_BASE"] = str(SCRATCH)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import runner  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


runner.VENV_PY = Path(sys.executable)
(SCRATCH / "app").mkdir(exist_ok=True)

# success: output file written, exits fine, failure tail not needed
outdir = SCRATCH / "logs"
runner.LOG_DIR = outdir
runner.run_scraper(["-c", "print('scraper said this')"])
saved = sorted(outdir.glob("scraper-*.log"))
check("success run leaves one output file", len(saved) == 1)
check("output file holds the scraper's words",
      saved and "scraper said this" in saved[0].read_text())

# failure: exits non-zero, still saves everything, raises
try:
    runner.run_scraper(
        ["-c", "print('line-a'); print('fatal marker', ); import sys; sys.exit(2)"])
    raised = False
except RuntimeError:
    raised = True
check("non-zero scraper raises RuntimeError", raised)
files = sorted(outdir.glob("scraper-*.log"))
check("failure run saved its output too", len(files) == 2)
body = files[-1].read_text()
check("saved file holds the fatal line",
      "line-a" in body and "fatal marker" in body)

# the stderr path lands in the same file
runner.run_scraper(["-c", "import sys; sys.stderr.write('stderr-marker\\n')"])
body = sorted(outdir.glob("scraper-*.log"))[-1].read_text()
check("stderr is captured in the same file", "stderr-marker" in body)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if fails else 0)
