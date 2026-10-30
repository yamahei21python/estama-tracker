"""Tests for the archive retention policy: only dated archive keys are
considered, newer-than-N days and Mondays are kept, and it is off unless
storage.json configures it. Exercises the local-mirror backend end to end.

Run: python3.11 tests/test_archive_retention.py
"""
import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="estama-retention-test-"))
atexit.register(shutil.rmtree, SCRATCH, True)
os.environ["ESTAMA_VM_BASE"] = str(SCRATCH)

spec = importlib.util.spec_from_file_location("runner", ROOT / "runner.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

JST = ZoneInfo("Asia/Tokyo")
fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


# ------------------------------------------------- pure selection logic
keys = [
    "archive/estama-2026-10-30.db.gz",  # Friday, today
    "archive/estama-2026-10-26.db.gz",  # Monday, inside window anyway
    "archive/estama-2026-10-10.db.gz",  # Saturday, 20d -> kept
    "archive/estama-2026-09-28.db.gz",  # Monday, 32d -> weekly kept
    "archive/estama-2026-09-27.db.gz",  # Sunday, 33d -> stale
    "archive/estama-2026-09-25.db.gz",  # Friday, 35d -> stale
    "estama.db.gz",                     # not an archive
    "archive/notes.txt",                # not dated
    "archive/estama-not-a-date.db.gz",  # not dated
]
today = datetime(2026, 10, 30, tzinfo=JST).date()
stale = r.stale_archive_keys(keys, today, 30, weekly=True)
check("stale: old non-Monday archives selected",
      stale == ["archive/estama-2026-09-25.db.gz",
                "archive/estama-2026-09-27.db.gz"])
check("stale: Monday outside window kept",
      "archive/estama-2026-09-28.db.gz" not in stale)
check("stale: inside window kept",
      "archive/estama-2026-10-10.db.gz" not in stale)
check("stale: non-archive keys ignored",
      all(k.startswith("archive/estama-20") for k in stale))
check("stale: weekly=False drops old Mondays too",
      "archive/estama-2026-09-28.db.gz"
      in r.stale_archive_keys(keys, today, 30, weekly=False))

# -------------------------------------------------- disabled by default
check("retention off without config",
      r.prune_archives(datetime(2026, 10, 30, tzinfo=JST)) == [])

# --------------------------------------- local-mirror end-to-end deletion
arch = SCRATCH / "r2_local" / "archive"
arch.mkdir(parents=True)
for key in keys:
    if key.startswith("archive/estama-20"):
        p = SCRATCH / "r2_local" / key
        p.write_bytes(b"x")
(SCRATCH / "storage.json").write_text(json.dumps(
    {"archive_retention": {"days": 30, "weekly": True}}))
removed = r.prune_archives(datetime(2026, 10, 30, tzinfo=JST))
check("retention deletes stale keys",
      removed == ["archive/estama-2026-09-25.db.gz",
                  "archive/estama-2026-09-27.db.gz"])
left = sorted(str(p.relative_to(SCRATCH / "r2_local")) for p in arch.iterdir())
check("retention keeps the rest",
      left == ["archive/estama-2026-09-28.db.gz",
               "archive/estama-2026-10-10.db.gz",
               "archive/estama-2026-10-26.db.gz",
               "archive/estama-2026-10-30.db.gz"])
check("retention never touches the live key",
      not (SCRATCH / "r2_local" / "estama.db").exists())

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
