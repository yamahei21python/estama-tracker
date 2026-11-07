"""Per-process run state (mutable; set by the CLI, read by publish).

A single active job at a time (guarded by LOCK_FILE), so a tiny mutable
context is fine. Kept out of env.py so modules that only read paths
never touch it.
"""
from __future__ import annotations

from datetime import datetime

CURRENT_RUN_ID: int | None = None   # run_log id of the active job
RUN_STARTED_AT: datetime | None = None  # naive JST; set right before the job
