"""Tests for the repo policy (config/policy.json).

config/policy.json is the single source of truth for the retention
windows: the VM clones it, GitHub Actions sees it in CI, and the
storage.json on each host may only override a single value. Without it,
the GHA runner (whose generated storage.json has no db_retention key)
would silently never prune - the live DB would grow past the Worker's
100MB gzip limit.

Run: python3 tests/test_policy.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import policy  # noqa: E402
import retention  # noqa: E402

SCRATCH = Path(tempfile.mkdtemp(prefix="estama-policy-test-"))
fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


# ----------------------------------------------- the shipped file
raw = json.loads((ROOT / "config" / "policy.json").read_text(encoding="utf-8"))
check("repo policy exists and is an object", isinstance(raw, dict))
dbr = raw.get("db_retention") or {}
check("policy keeps the documented windows",
      dbr.get("snapshots_days") == 15
      and dbr.get("slot_events_days") == 30
      and dbr.get("daily_days") == 365
      and dbr.get("rankings_days") == 365
      and dbr.get("scrape_log_days") == 90
      and dbr.get("run_log_days") == 365)
check("policy ships no archive retention (deletion stays opt-in)",
      "archive_retention" not in raw)

# ----------------------------------------------- merge semantics
check("no storage override -> policy values",
      policy.db_retention_config(None) == dbr
      and policy.db_retention_config({}) == dbr)
override = policy.db_retention_config({"snapshots_days": 3})
check("storage override wins for that key only",
      override["snapshots_days"] == 3 and override["daily_days"] == 365)
check("storage zero disables a table even with a policy default",
      policy.db_retention_config({"snapshots_days": 0})["snapshots_days"] == 0)

merged = policy.db_retention_config({"snapshots_days": "15d"})
cfg = retention.retention_config(merged)
check("a malformed storage value disables only that table",
      cfg["snapshots_days"] is None and cfg["daily_days"] == 365)

# the gate the GHA runner sees: no storage.json key, policy fills in
check("GHA without a storage.json override still gets the policy windows",
      retention.retention_config(policy.db_retention_config(None)) is not None)

# ----------------------------------------------- failure modes
os.environ["ESTAMA_POLICY_PATH"] = str(SCRATCH / "absent.json")
try:
    check("missing policy file -> no defaults, disabled",
          policy.db_retention_config(None) is None
          and policy.archive_retention_config(None) == {})
finally:
    del os.environ["ESTAMA_POLICY_PATH"]

garbage = SCRATCH / "garbage.json"
garbage.write_text("{ not json", encoding="utf-8")
os.environ["ESTAMA_POLICY_PATH"] = str(garbage)
try:
    check("corrupt policy disables everything, fails silent",
          policy.db_retention_config(None) is None
          and policy.archive_retention_config(None) == {})
    merged_storage_only = policy.db_retention_config(
        {"snapshots_days": 7})
    check("corrupt policy falls back to the host override only",
          merged_storage_only == {"snapshots_days": 7})
finally:
    del os.environ["ESTAMA_POLICY_PATH"]

list_policy = SCRATCH / "list.json"
list_policy.write_text("[1, 2, 3]", encoding="utf-8")
os.environ["ESTAMA_POLICY_PATH"] = str(list_policy)
try:
    check("non-object policy disables everything",
          policy.db_retention_config(None) is None)
finally:
    del os.environ["ESTAMA_POLICY_PATH"]

# ----------------------------------------------- archive retention
check("archive retention merges storage.json override onto an empty base",
      policy.archive_retention_config({"days": 30, "weekly": True})
      == {"days": 30, "weekly": True})
check("archive retention unset stays empty (deletion is opt-in)",
      policy.archive_retention_config(None) == {}
      and policy.archive_retention_config({}) == {})

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
import shutil  # noqa: E402

shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if fails else 0)
