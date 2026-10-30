"""Tests for runner.py safeguards: DB verification, business-date
archive, staged upload promotion, and the single-instance lock.

Self-contained: builds a tiny DB from schema.sql in a temp dir.
Run: python3 tests/test_runner.py
"""
import atexit
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="estama-runner-test-"))
atexit.register(shutil.rmtree, SCRATCH, True)
JST = ZoneInfo("Asia/Tokyo")

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

# a site flip before the last checkpoint leaves rows for BOTH the closed
# day and the new business date in the DB; MAX(target_date) is then the
# new date, so the archive must key on expected-1, never on MAX
flip_db = SCRATCH / "app" / "flip.db"
con = sqlite3.connect(flip_db)
con.executescript((ROOT / "app" / "schema.sql").read_text())
con.execute("INSERT INTO shops VALUES (1, '店A', '梅田', NULL, NULL)")
for d in ("2026-10-26", "2026-10-27"):
    con.execute(
        "INSERT INTO snapshots (shop_id, cast_name, status_norm,"
        " target_date, fetched_time) VALUES (1, 'a', 'available', ?, '10:00')",
        (d,))
con.commit()
con.close()
orig_db, orig_state = r.DB, r.STATE_FILE
r.DB = flip_db
r.STATE_FILE = SCRATCH / "archive_state_flip.json"
try:
    key_flip = r.maybe_archive(now)
finally:
    r.DB, r.STATE_FILE = orig_db, orig_state
check("archive keys on closed date even after a site flip",
      key_flip == "archive/estama-2026-10-26.db")
check("archive records the closed date in state", json.loads(
    (SCRATCH / "archive_state_flip.json").read_text()
)["last_archived"] == "2026-10-26")

r.upload_db()
live = SCRATCH / "r2_local" / "estama.db"
check("live key exists after upload", live.exists())
check("upload writes the live DB only (one object, no staging key)",
      sorted(p.name for p in (SCRATCH / "r2_local").iterdir())
      == ["archive", "estama.db"])
con = sqlite3.connect(live)
check("uploaded DB readable with 3 rows",
      con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 3)
con.close()

# upload_db: a failed post-write verification retries once, then
# restores the previous live blob so the branch never keeps a bad DB.
puts, restores = [], []
orig = (r.backend_name, r.storage_put, r.storage_verify,
        r.gh_live_blob, r.gh_restore)
r.backend_name = lambda: "github"
r.storage_put = lambda key, f: puts.append(key)
r.storage_verify = lambda key, f: False
r.gh_live_blob = lambda key: "prevsha"
r.gh_restore = lambda key, sha: restores.append((key, sha))
try:
    r.upload_db()
    check("upload raises when verification keeps failing", False)
except IOError:
    check("upload raises when verification keeps failing", True)
finally:
    (r.backend_name, r.storage_put, r.storage_verify,
     r.gh_live_blob, r.gh_restore) = orig
check("failed verification retries the upload once",
      puts == ["estama.db", "estama.db"])
check("failed verification restores the previous live blob",
      restores == [("estama.db", "prevsha")])

# no previous blob (first publish ever): the bad live key is deleted so
# the next run does not mistake an unverifiable object for a good one
deletes = []
orig = (r.backend_name, r.storage_put, r.storage_verify,
        r.gh_live_blob, r.gh_restore, r.storage_delete)
r.backend_name = lambda: "github"
r.storage_put = lambda key, f: None
r.storage_verify = lambda key, f: False
r.gh_live_blob = lambda key: None
r.gh_restore = lambda key, sha: None
r.storage_delete = lambda key: deletes.append(key)
try:
    r.upload_db()
    check("upload raises (no previous blob)", False)
except IOError:
    check("upload raises (no previous blob)", True)
finally:
    (r.backend_name, r.storage_put, r.storage_verify,
     r.gh_live_blob, r.gh_restore, r.storage_delete) = orig
check("failed verification with no previous blob deletes the live key",
      deletes == ["estama.db"])

holder = subprocess.Popen(
    [sys.executable, "-c",
     f"import fcntl,time; f=open({str(SCRATCH / 'runner.lock')!r},'w');"
     " fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(8)"],
    stdout=subprocess.PIPE, text=True)
# wait for the holder to actually own the lock instead of sleeping a
# fixed second: same guarantee, no idle time in the suite
assert holder.stdout.readline().strip() == "locked", "lock holder did not start"
env = dict(os.environ, ESTAMA_VM_BASE=str(SCRATCH))
p = subprocess.run([sys.executable, str(ROOT / "runner.py"), "checkpoint"],
                   capture_output=True, text=True, env=env, timeout=30)
check("overlap run exits 0 with SKIP", p.returncode == 0 and "SKIP" in p.stdout)
holder.terminate()
holder.wait(timeout=10)

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

# publish failure after a good job: the run row must say fail (the
# artifact is stale) and main() must exit non-zero for cron
orig = (r.job_checkpoint, r.publish, sys.argv)
r.job_checkpoint = lambda: "snapshots +1"
r.publish = lambda: (_ for _ in ()).throw(IOError("boom"))
sys.argv = ["runner.py", "checkpoint"]
try:
    rc = r.main()
finally:
    r.job_checkpoint, r.publish, sys.argv = orig
con = sqlite3.connect(db)
st = con.execute("SELECT status, detail FROM run_log"
                 " ORDER BY run_id DESC LIMIT 1").fetchone()
con.close()
check("publish failure exits 1 and marks the run fail",
      rc == 1 and st[0] == "fail" and "publish failed" in st[1])

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
