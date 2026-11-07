"""Scraper process execution."""
from __future__ import annotations
import os
import subprocess
from runtrack.env import APP, LOG_DIR, VENV_PY, log
from runtrack import state
from common import now_jst

def run_scraper(args: list[str], timeout: int = 1800) -> None:
    env = dict(os.environ)
    if state.CURRENT_RUN_ID is not None:
        env["ESTAMA_RUN_ID"] = str(state.CURRENT_RUN_ID)  # scraper -> scrape_log join
    proc = subprocess.run(
        [str(VENV_PY), *args],
        cwd=APP, capture_output=True, text=True, timeout=timeout, env=env,
    )
    combined = (proc.stdout + "\n" + proc.stderr).strip()
    # Full scraper output goes to a log file: the cron only sees the
    # runner's summary, so without this a failed hour is undebuggable.
    outfile = LOG_DIR / f"scraper-{now_jst():%Y%m%d-%H%M%S-%f}.log"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        outfile.write_text(combined + "\n", encoding="utf-8")
        log(f"  scraper output -> {outfile}")
    except OSError as exc:
        log(f"  scraper output could not be saved ({exc})")
    ok = proc.returncode == 0
    tail_size = 3 if ok else 40
    for line in combined.splitlines()[-tail_size:]:
        log(f"  scraper: {line}")
    if not ok:
        raise RuntimeError(f"scraper exited {proc.returncode}")
