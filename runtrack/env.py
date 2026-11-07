"""Paths, sizes and logging shared by the runner.

Everything that used to be a module-level constant in runner.py. The
VM layout is fixed: repo root = BASE, the app tree in `app/`, the
SQLite DB is `app/estama.db`. `ESTAMA_VM_BASE` overrides for tests.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE = Path(os.environ.get("ESTAMA_VM_BASE", Path(__file__).resolve().parent.parent))
APP = BASE / "app"
DB = APP / "estama.db"
CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(CODE_DIR / "app"))

VENV_PY = BASE / "venv" / "bin" / "python"
if not VENV_PY.exists():
    # GitHub Actions / bare checkouts have no venv; use the interpreter
    # that is running this file.
    VENV_PY = Path(sys.executable)

LOCK_FILE = BASE / "runner.lock"
STATE_FILE = BASE / "archive_state.json"
LOG_DIR = BASE / "logs"
STAGING_DIR = BASE / "staging"
DASHBOARD = APP / "dashboard.html"

LIVE_KEY = "estama.db"
# Object key holding the live DB as it was *before* the current publish.
# Written with a server-side R2 copy, so it can be rolled back without
# downloading anything. Overwritten on every publish.
PREV_OBJ = "estama.db.prev.gz"
# Small object the schedule/backup logic reads. Written after every
# successful publish so a stateless runner (GitHub Actions) can tell
# whether the VM is still doing its job.
HEARTBEAT_KEY = "status/last_ok.json"

BAN_AFTER_DAYS = 20   # observation span before a never-sold-out shop is banned
BAN_MIN_OBS_DAYS = 15  # minimum distinct observed days (coverage guard)
SHOPS_JSON = APP / "shops.json"
BANNED_JSON = APP / "banned_shops.json"

sys.path.insert(0, str(CODE_DIR / "app"))

from common import now_jst  # noqa: E402


def log(msg: str) -> None:
    ts = now_jst().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / f"runner-{now_jst():%Y-%m-%d}.log", "a") as fh:
        fh.write(line + "\n")
