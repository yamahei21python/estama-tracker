"""Backup takeover guard."""
from __future__ import annotations
from runtrack.heartbeat import read_heartbeat, heartbeat_age_min
from runtrack.env import log
from common import now_jst

def job_guard(max_age_min: float) -> int:
    """Exit 0 when the VM is keeping up (skip), 3 when it is not (run)."""
    payload = read_heartbeat()
    age = heartbeat_age_min(payload, now_jst())
    if age is None:
        log("guard: no usable heartbeat -> taking over")
        return 3
    if age <= max_age_min:
        log(f"guard: heartbeat {age:.0f} min old "
            f"(job={payload.get('job')}) -> VM is alive, skip")
        return 0
    log(f"guard: heartbeat {age:.0f} min old (> {max_age_min:.0f}) -> take over")
    return 3
