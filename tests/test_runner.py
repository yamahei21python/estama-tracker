"""Tests for runner.py safeguards: DB verification, business-date
archive, staged upload promotion, and the single-instance lock.

Self-contained: builds a tiny DB from schema.sql in a temp dir.
Run: python3 tests/test_runner.py
"""
import importlib.util
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path("/tmp/estama-runner-test")
JST = ZoneInfo("Asia/Tokyo")

subprocess.run(["rm", "-rf", str(SCRATCH)])
(SCRATCH / "app").mkdir(parents=True)
os.environ["ESTAMA_VM_BASE"] = str(SCRATCH)

spec = importlib.util.spec_from_file_location("runner", ROOT / "runner.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

# tiny DB: 2 shops, 3 snapshot rows on business date 2026-10-26
db = SCRATCH / "app" / "estama.db"
con = sqlite3.connect(db)
con.executescript((ROOT / "app" / "schema.sql").read_text())
con.execute("INSERT INTO shops VALUES (1, '店A', '梅田', NULL, NULL)")
con.execute("INSERT INTO shops VALUES (2, '店B', '難波', NULL, NULL)")
for i, cast in enumerate(("a", "b", "c")):
    con.execute(
        "INSERT INTO snapshots (shop_id, cast_name, status_norm, target_date,"
        " fetched_time) VALUES (1, ?, 'available', '2026-10-26', ?)",
        (cast, f"1{i}:00"))
con.commit()
con.close()
(SCRATCH / "app" / "shops.json").write_text('{"shops": []}')

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


stats = r.verify_db(db)
check("verify ok on good DB", stats["shops"] == 2 and stats["snapshots"] == 3)

bad = SCRATCH / "app" / "bad.db"
data = bytearray(db.read_bytes())
for i in range(100, 400):
    data[i] = 0xFF
bad.write_bytes(bytes(data))
try:
    r.verify_db(bad)
    check("verify refuses corrupt DB", False)
except Exception:
    check("verify refuses corrupt DB", True)

now = datetime(2026, 10, 27, 10, 0, tzinfo=JST)
key = r.maybe_archive(now)
check("archive fires for closed business date", key == "archive/estama-2026-10-26.db")
check("archive object exists", (SCRATCH / "r2_local" / "archive" / "estama-2026-10-26.db").exists())
check("archive fires only once", r.maybe_archive(now) is None)
early = datetime(2026, 10, 26, 4, 0, tzinfo=JST)
check("no archive before the flip", r.maybe_archive(early) is None)

r.upload_db()
live = SCRATCH / "r2_local" / "estama.db"
check("live key exists after upload", live.exists())
check("staging key cleaned up", not (SCRATCH / "r2_local" / "estama.db.staging").exists())
con = sqlite3.connect(live)
check("uploaded DB readable with 3 rows",
      con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 3)
con.close()

holder = subprocess.Popen([sys.executable, "-c",
                           "import fcntl,time; f=open('/tmp/estama-runner-test/runner.lock','w');"
                           " fcntl.flock(f, fcntl.LOCK_EX); time.sleep(8)"])
import time
time.sleep(1)
env = dict(os.environ, ESTAMA_VM_BASE=str(SCRATCH))
p = subprocess.run([sys.executable, str(ROOT / "runner.py"), "checkpoint"],
                   capture_output=True, text=True, env=env, timeout=30)
check("overlap run exits 0 with SKIP", p.returncode == 0 and "SKIP" in p.stdout)
holder.terminate()

# run_log: the SKIP above must have been recorded in the DB
con = sqlite3.connect(db)
con.executescript((ROOT / "app" / "schema.sql").read_text())  # ensure run_log exists
con.commit()
rows = con.execute(
    "SELECT job, status, detail FROM run_log ORDER BY run_id").fetchall()
check("run_log records lock SKIP",
      any(r[0] == "checkpoint" and r[1] == "skip" for r in rows))

# run_log open/close lifecycle
rid = r.run_log_open("prune")
r.run_log_close(rid, "ok", "banned=0")
row = con.execute(
    "SELECT job, status, detail, finished_at FROM run_log WHERE run_id=?",
    (rid,)).fetchone()
check("run_log open/close lifecycle",
      row[0] == "prune" and row[1] == "ok" and row[2] == "banned=0"
      and row[3] is not None)
con.close()

# scrape_log: per-shop audit links back to its run via ESTAMA_RUN_ID
sys.path.insert(0, str(ROOT / "app"))
from common import log_scrape  # noqa: E402

os.environ["ESTAMA_RUN_ID"] = str(rid)
log_scrape(db, 40900, "ok", 42)
log_scrape(db, 39488, "fail", 0, "HTTP 503")
os.environ.pop("ESTAMA_RUN_ID")
log_scrape(db, 30039, "empty", 0)
con = sqlite3.connect(db)
rows = con.execute(
    "SELECT run_id, shop_id, status, rows, detail FROM scrape_log"
    " ORDER BY log_id").fetchall()
check("scrape_log links run + records outcomes",
      rows[0][0] == rid and rows[0][1] == 40900 and rows[0][3] == 42
      and rows[1][2] == "fail" and "503" in rows[1][4]
      and rows[2][0] is None and rows[2][2] == "empty")
con.close()

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
