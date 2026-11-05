"""Static + behavioural checks for the stateless GitHub Actions plan.

Two things are tested here:

1. `.github/workflows/collect.yml` keeps the properties the plan relies
   on. Text assertions on purpose: PyYAML is not a project dependency
   (CI installs only requests + beautifulsoup4), and these are order /
   presence invariants, not schema checks.
       - manual dispatch only for now (no schedule until it is proven)
       - one writer: concurrency group + cancel-in-progress: false
       - the token is never echoed and comes from secrets
       - pull runs before the job, so the job never starts from an empty DB
2. `runner.py guard` semantics, including the CLI contract: an unknown
   state must take over (exit 3), never silently skip.

Run: python3 tests/test_collect_workflow.py
"""
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORKFLOW = ROOT / ".github" / "workflows" / "collect.yml"
fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


text = WORKFLOW.read_text(encoding="utf-8")

# ------------------------------------------------------------- triggers
check("workflow file exists", WORKFLOW.exists())
check("manual dispatch exists", "workflow_dispatch:" in text)
check("no schedule yet (manual rollout first)",
      not re.search(r"^\s*schedule:", text, re.M))
check("job choice is constrained", "options: [checkpoint, ranking]" in text)
check("backup mode exists", "options: [primary, backup]" in text)

# ---------------------------------------------------------- one writer
check("concurrency block present", re.search(r"^concurrency:", text, re.M))
check("single writer group", "group: estama-db-writer" in text)
check("never cancel a run in progress", "cancel-in-progress: false" in text)
check("no other concurrency group",
      len(re.findall(r"^\s*group:", text, re.M)) == 1)

# ------------------------------------------------------------- secrets
check("token comes from a repo secret",
      "${{ secrets.GATEWAY_TOKEN }}" in text)
check("base URL comes from a repo variable",
      "${{ vars.GATEWAY_BASE }}" in text)
check("token is never printed", "echo $GATEWAY_TOKEN" not in text
      and "echo ${{ secrets.GATEWAY_TOKEN }}" not in text)
check("storage.json is generated, not committed",
      '"gateway_base": base' in text)

# --------------------------------------------------------- step order
pull_at = text.find("python runner.py pull")
job_at = text.find("python runner.py '${{ inputs.job }}'")
check("pull step exists", pull_at > 0)
check("job step exists", job_at > 0)
check("pull runs before the job", 0 < pull_at < job_at)
guard_at = text.find("python runner.py guard")
check("guard runs before pull", 0 < guard_at < pull_at)
check("guard only in backup mode", "if: inputs.mode == 'backup'" in text)
check("backup skip is honoured by later steps",
      "steps.guard.outputs.run != 'false'" in text)
check("runtime timeout set", re.search(r"timeout-minutes:\s*\d+", text))

# ------------------------------------------------------ YAML structure
# PyYAML is optional (CI installs it; a bare checkout may not have it).
try:
    import yaml
except ImportError:  # pragma: no cover - depends on the environment
    print("SKIP yaml structure (PyYAML not installed)")
else:
    doc = yaml.safe_load(text)
    trigger = doc.get("on", doc.get(True)) or {}
    check("yaml: only workflow_dispatch triggers",
          set(trigger) == {"workflow_dispatch"})
    conc = doc.get("concurrency") or {}
    check("yaml: concurrency group", conc.get("group") == "estama-db-writer")
    check("yaml: cancel-in-progress is false",
          conc.get("cancel-in-progress") is False)
    check("yaml: read-only permissions",
          (doc.get("permissions") or {}).get("contents") == "read")
    job = (doc.get("jobs") or {}).get("collect") or {}
    check("yaml: single job", list(doc.get("jobs") or {}) == ["collect"])
    check("yaml: job timeout is bounded", job.get("timeout-minutes") == 50)
    steps = job.get("steps") or []
    runs = [str(s.get("run", "")) for s in steps]
    pull_i = next(i for i, r in enumerate(runs) if "runner.py pull" in r)
    job_i = next(i for i, r in enumerate(runs) if "runner.py '${{ inputs.job }}'" in r)
    check("yaml: pull step before job step", pull_i < job_i)
    check("yaml: checkpoint never runs without a DB pull",
          all("checkpoint" not in r for r in runs[:pull_i]))
    env = job.get("env") or {}
    check("yaml: job env carries the token",
          env.get("GATEWAY_TOKEN") == "${{ secrets.GATEWAY_TOKEN }}")

# ------------------------------------------------------- guard semantics
SCRATCH = Path(tempfile.mkdtemp(prefix="estama-collect-test-"))
os.environ["ESTAMA_VM_BASE"] = str(SCRATCH)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from common import now_jst  # noqa: E402

import runner  # noqa: E402

now = now_jst()
fresh = {"job": "checkpoint", "finished_at": now.isoformat()}

check("exactly max-age is still fresh",
      runner.heartbeat_age_min(fresh, now) <= 90)
check("one second past max-age is stale",
      runner.heartbeat_age_min(
          {"finished_at": (now - timedelta(minutes=90, seconds=1)).isoformat()},
          now) > 90)
check("a 15-minute-old heartbeat is fresh",
      runner.heartbeat_age_min(
          {"finished_at": (now - timedelta(minutes=15)).isoformat()}, now) <= 90)
check("a naive stamp is read as JST (run_log convention)",
      runner._as_jst_naive("2026-11-05T00:00:00")
      == datetime(2026, 11, 5, 0, 0))

# ------------------------------------------------- CLI guard contract
# No heartbeat object exists in the empty local mirror: a stateless
# runner must take over (3), never skip (0).


def run_main(argv):
    old = sys.argv
    sys.argv = ["runner.py", *argv]
    try:
        return runner.main()
    finally:
        sys.argv = old


check("unknown job -> exit 2", run_main(["nope"]) == 2)
check("guard with no heartbeat -> exit 3 (take over)",
      run_main(["guard", "--max-age", "90"]) == 3)
check("guard with a bad --max-age -> exit 2",
      run_main(["guard", "--max-age"]) == 2)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if fails else 0)
