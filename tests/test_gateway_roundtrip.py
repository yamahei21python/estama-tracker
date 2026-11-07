"""End-to-end test of the stateless runner (GitHub Actions plan).

Runs a real HTTP fake of the Worker gateway contract (token auth, PUT,
GET, HEAD with X-Object-Size + ETag, DELETE) on 127.0.0.1, then drives
the actual code paths the workflow uses:

    pull  : storage_get -> gzip decode -> verify_db
    run   : (job body is the existing, VM-proven runner)
    push  : storage_put + ETag/MD5 verification (already covered)
    guard : heartbeat object decides whether a backup takes over

The point is the round trip the VM never needed: download. The upload
path was already proven on the VM; the return leg is what a stateless
runner depends on.

Run: python3 tests/test_gateway_roundtrip.py
"""
import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="estama-gwrt-test-"))
from harness import check, fails  # noqa: E402


# ------------------------------------------------ fake Worker gateway
TOKEN = "test-token"
STORE = {}
SEEN_AUTH = []
FAIL_GET = {"key": None}   # serve a corrupt body for this key


def md5(data):
    return hashlib.md5(data).hexdigest()


class FakeGateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _auth(self):
        auth = self.headers.get("Authorization")
        SEEN_AUTH.append(auth)
        return auth == f"Bearer {TOKEN}"

    def _key(self):
        return self.path.lstrip("/").split("?", 1)[0]

    def do_PUT(self):
        if not self._auth():
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length") or 0)
        STORE[self._key()] = self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._auth():
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        key = self._key()
        if FAIL_GET["key"] == key:
            body = b"not gzip at all"
        else:
            body = STORE.get(key)
        if body is None:
            body = b"not found"
            code = 404
        else:
            code = 200
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        if not self._auth():
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = STORE.get(self._key())
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("X-Object-Size", str(len(body)))
        self.send_header("ETag", f'"{md5(body)}"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        if not self._auth():
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        STORE.pop(self._key(), None)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if not self._auth():
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path != "/__copy":
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            payload = json.loads(raw)
        except ValueError:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        src, dst = payload.get("src"), payload.get("dst")
        if not src or not dst:
            self.send_response(400)
        elif src not in STORE:
            self.send_response(404)
        else:
            STORE[dst] = STORE[src]
            self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE_URL = f"http://127.0.0.1:{srv.server_address[1]}"

# ------------------------------------------------ environment
os.environ["ESTAMA_VM_BASE"] = str(SCRATCH)   # runner's BASE
os.environ["GATEWAY_TOKEN"] = TOKEN
(SCRATCH / "storage.json").write_text(json.dumps({
    "backend": "gateway", "gateway_base": BASE_URL,
}))

spec = importlib.util.spec_from_file_location("storage", ROOT / "storage.py")
storage = importlib.util.module_from_spec(spec)
sys.modules["storage"] = storage
spec.loader.exec_module(storage)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))
import common  # noqa: E402

import runner  # noqa: E402

# ------------------------------------------------ a real (small) DB
src = SCRATCH / "src.db"
conn = common.connect_db(str(src))
conn.execute("INSERT INTO shops VALUES (1, 'テスト店', '梅田', NULL, NULL)")
conn.execute(
    """INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,
           status_norm, target_date, fetched_time, fetched_at)
       VALUES (1, 'キャストA', 100, 'available', 'available',
               '2026-11-05', '10:00', '2026-11-05 10:00:00 JST')""")
conn.commit()
conn.close()
raw = src.read_bytes()
check("fixture DB is small but valid", 1024 < len(raw) < 200_000)

# ------------------------------------------------ download leg
check("storage_get is implemented for gateway",
      storage.backend_name() == "gateway")
check("missing key -> False", storage.storage_get("nope.db", SCRATCH / "x") is False)

storage.storage_put("estama.db", src)
check("upload stored the gz object", "estama.db.gz" in STORE)
check("stored payload is gzipped",
      gzip.decompress(STORE["estama.db.gz"]) == raw)
# The stored payload must be the pinned-header form: the VM (Linux)
# and a Mac/GHA reader have to derive the same MD5 from the same DB,
# otherwise storage_verify false-fails across hosts (live 2026-11-05).
check("stored payload is gz_deterministic(raw)",
      STORE["estama.db.gz"] == storage.gz_deterministic(raw))
check("gzip OS header byte is pinned to 0x03",
      storage.gz_deterministic(raw)[9] == 0x03)
# Simulate a host whose gzip writer stamps a different OS byte
# (macOS/Windows emit 0xFF): the pinned header must make both hosts
# derive the same payload and therefore the same ETag.
real_compress = gzip.compress


def mac_compress(data, *a, **kw):
    out = bytearray(real_compress(data, *a, **kw))
    out[9] = 0xFF
    return bytes(out)


gzip.compress = mac_compress
try:
    pinned_from_mac = storage.gz_deterministic(raw)
finally:
    gzip.compress = real_compress
check("a foreign OS byte normalizes to the same MD5",
      pinned_from_mac == storage.gz_deterministic(raw)
      and pinned_from_mac[9] == 0x03)
check("Bearer token sent directly", SEEN_AUTH[-1] == f"Bearer {TOKEN}")

# ------------------------------------------- connector (VM) auth path
# The VM has no token on disk: the connector surrogate injects the
# header on egress. Prove that path still works after adding the
# direct-token branch (a stub module stands in for the real one).
cred = SCRATCH / "credlib"
cred.mkdir()
(cred / "dynamic_credentials.py").write_text(
    "import json, pathlib\n"
    "RECORD = pathlib.Path(__file__).parent / 'calls.json'\n"
    "def add_surrogate_to_request(req, connector, allowed_hosts=None):\n"
    "    assert req.get_header('Authorization') is None, 'token sent twice'\n"
    "    RECORD.write_text(json.dumps({'connector': connector,\n"
    "                                  'hosts': allowed_hosts}))\n"
    "    req.add_header('Authorization', 'Bearer test-token')\n",
    encoding="utf-8")
old_lib = storage.GH_CRED_LIB
storage.GH_CRED_LIB = str(cred)
os.environ.pop("GATEWAY_TOKEN", None)
try:
    via = SCRATCH / "via-connector.db"
    connector_ok = storage.storage_get("estama.db", via)
finally:
    storage.GH_CRED_LIB = old_lib
    os.environ["GATEWAY_TOKEN"] = TOKEN
check("connector path downloads without GATEWAY_TOKEN", connector_ok)
check("connector path returns the same bytes", via.read_bytes() == raw)
record = json.loads((cred / "calls.json").read_text())
check("connector name is unchanged", record["connector"] == "custom.estama-r2")
check("connector is scoped to the gateway host",
      record["hosts"] == ["127.0.0.1"])

out = SCRATCH / "downloaded.db"
check("download returns True", storage.storage_get("estama.db", out) is True)
check("download is byte-identical", out.read_bytes() == raw)
check("verify proves the stored object", storage.storage_verify("estama.db", out) is True)

# A mismatch must say why: a bare False in a cron log hides whether the
# upload broke or we compared the wrong bytes.
import contextlib  # noqa: E402
import io  # noqa: E402

other = SCRATCH / "other.db"
other.write_bytes(raw + b"\0")
err = io.StringIO()
with contextlib.redirect_stderr(err):
    mismatch_ok = storage.storage_verify("estama.db", other)
check("verify rejects different content", mismatch_ok is False)
check("verify explains the mismatch on stderr",
      "MD5 mismatch" in err.getvalue())
check("download leaves no .part file",
      not list(out.parent.glob("*.part")))
check("downloaded DB passes verify_db",
      runner.verify_db(out)["snapshots"] == 1)

# a corrupt gzip must fail loudly, not leave a half-written DB
FAIL_GET["key"] = "estama.db.gz"
before = out.read_bytes()
try:
    storage.storage_get("estama.db", out)
    corrupt_raised = False
except IOError:
    corrupt_raised = True
FAIL_GET["key"] = None
check("corrupt gzip raises IOError", corrupt_raised)
check("corrupt download left the old file intact", out.read_bytes() == before)

# wrong token must not be able to download
os.environ["GATEWAY_TOKEN"] = "wrong"
try:
    storage.storage_get("estama.db", SCRATCH / "unauth.db")
    unauth_raised = False
except IOError:
    unauth_raised = True
os.environ["GATEWAY_TOKEN"] = TOKEN
check("401 raises instead of writing", unauth_raised)
check("no file written on 401", not (SCRATCH / "unauth.db").exists())

# an existing file must survive a failed download untouched
keep = SCRATCH / "keepme.db"
keep.write_bytes(b"previous good bytes")
os.environ["GATEWAY_TOKEN"] = "wrong"
try:
    storage.storage_get("estama.db", keep)
except IOError:
    pass
os.environ["GATEWAY_TOKEN"] = TOKEN
check("401 leaves an existing file untouched",
      keep.read_bytes() == b"previous good bytes")

# ------------------------------ server-side rollback copy (/__copy)
# NOTE: runs before the "runner pull" section, so make the local DB
# first; the rollback test depends on app/estama.db existing.
pre_live = STORE.get("estama.db.gz")
check("live object present before copy test", pre_live is not None)
storage.storage_copy_object("estama.db.gz", "estama.db.prev.gz")
check("server-side copy stored under the new key",
      STORE.get("estama.db.prev.gz") == pre_live)
check("copy verify passes for an identical object",
      storage.storage_copy_verify("estama.db.prev.gz", "estama.db.gz") is True)
try:
    storage.storage_copy_object("missing.gz", "dst.gz")
    copy_missing_raised = False
except IOError:
    copy_missing_raised = True
check("missing source raises IOError", copy_missing_raised)
check("failed copy left no destination object", "dst.gz" not in STORE)

# A failed publish must roll the live object back, without a download.
# Patch the implementation module, which runner delegates to.
import runtrack.storage_ops as sto  # noqa: E402
original_verify = sto.storage_verify
sto.storage_verify = lambda *a, **kw: False
try:
    # upload_db() runs against the pulled DB, so put a known object
    # in place and expect it to come back after the rollback.
    runner.job_pull()                # app/estama.db now holds the src bytes
    storage.storage_put("estama.db", src)
    pre_upload = STORE["estama.db.gz"]
    try:
        runner.upload_db()
        rollback_raised = False
    except IOError:
        rollback_raised = True
finally:
    sto.storage_verify = original_verify
check("publish fails when verification never passes", rollback_raised)
check("live object was rolled back to the pre-publish copy",
      STORE.get("estama.db.gz") == pre_upload)
check("prev object was refreshed by the publish attempt",
      "estama.db.prev.gz" in STORE)
# put the live object back to what the rest of the suite expects
storage.storage_put("estama.db", src)

# ------------------------------------------------ runner pull
check("runner falls back when there is no venv", runner.VENV_PY.exists())
detail = runner.job_pull()
check("pull reports the snapshot count", "1 snapshots" in detail)
pulled = SCRATCH / "app" / "estama.db"
check("pull wrote app/estama.db", pulled.exists())
check("pull is byte-identical", pulled.read_bytes() == raw)
check("pull leaves no -wal/-shm", not list(pulled.parent.glob("estama.db-*")))

STORE.pop("estama.db.gz", None)
try:
    runner.job_pull()
    missing_raised = False
except RuntimeError:
    missing_raised = True
check("pull fails when the live key is gone", missing_raised)
storage.storage_put("estama.db", src)   # restore for the rest

# ------------------------------------------------ heartbeat / guard
runner.write_heartbeat("checkpoint", "test detail")
hb = runner.read_heartbeat()
check("heartbeat round-trips through the gateway",
      hb.get("job") == "checkpoint" and hb.get("snapshots") == 1)
check("heartbeat carries the DB size", hb.get("db_bytes") == len(raw))
check("heartbeat timestamp is a JST run stamp",
      hb.get("finished_at", "").endswith(" JST")
      and runner._as_jst_naive(hb.get("finished_at")) is not None)

from datetime import timedelta  # noqa: E402

from common import now_jst  # noqa: E402

check("guard: fresh heartbeat -> skip (0)", runner.job_guard(90) == 0)
check("guard: too-old heartbeat -> take over (3)", runner.job_guard(-1) == 3)
check("age of a just-written heartbeat is ~0",
      abs(runner.heartbeat_age_min(hb, now_jst())) < 1)
check("age is None for a malformed timestamp",
      runner.heartbeat_age_min({"finished_at": "not-a-date"}, now_jst()) is None)
check("age is None without a timestamp",
      runner.heartbeat_age_min({}, now_jst()) is None)

storage.storage_delete(runner.HEARTBEAT_KEY)
check("guard: missing heartbeat -> take over (3)", runner.job_guard(90) == 3)
runner.write_heartbeat("ranking", "x")
check("guard does not require the same job name",
      runner.job_guard(90) == 0)

# ------------------------------------- VM adopts a newer remote DB
# A GitHub Actions backup may run while the VM is down. When the VM
# comes back, its stale copy must not overwrite the backup's rows
# (last writer wins), so the checkpoint pulls first when the remote
# heartbeat is newer than the local last successful run.
import sqlite3  # noqa: E402

src2 = SCRATCH / "src2.db"
shutil.copyfile(src, src2)
con = sqlite3.connect(src2)
con.execute(
    """INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,
           status_norm, target_date, fetched_time, fetched_at)
       VALUES (1, 'キャストB', 200, 'sold_out', 'sold_out',
               '2026-11-05', '11:00', '2026-11-05 11:00:00 JST')""")
con.commit()
con.close()
storage.storage_put("estama.db", src2)
runner.write_heartbeat("checkpoint", "backup run", "2026-11-05 11:00:00 JST")
check("stale local DB has no successful run yet",
      runner.local_last_ok() is None)
check("VM pulls when the remote is newer",
      runner._pull_if_remote_newer() is True)
pulled = SCRATCH / "app" / "estama.db"
check("adopted DB carries the backup's rows",
      runner.snapshot_count(pulled) == 2)

con = sqlite3.connect(pulled)
con.execute(
    """INSERT INTO run_log (job, started_at, finished_at, status)
       VALUES ('checkpoint', '2026-11-05 23:00:00',
               '2026-11-05 23:05:00', 'ok')""")
con.commit()
con.close()
check("VM does not pull when its own DB is newer",
      runner._pull_if_remote_newer() is False)
runner.write_heartbeat("checkpoint", "self", "2026-11-05 23:05:00 JST")
check("VM does not pull when the heartbeat is its own run stamp",
      runner._pull_if_remote_newer() is False)
con = sqlite3.connect(pulled)
kept = con.execute("SELECT COUNT(*) FROM run_log").fetchone()[0]
con.close()
check("no re-download happened (local run_log row survived)", kept == 1)

# run_log writes "... JST"; the heartbeat writes ISO+offset. Both must
# be comparable or the VM would re-download the DB every single hour.
from datetime import datetime  # noqa: E402

check("run_log JST-suffixed timestamps parse",
      runner._as_jst_naive("2026-11-05 23:05:00 JST")
      == datetime(2026, 11, 5, 23, 5))
check("ISO offset timestamps parse to the same wall clock",
      runner._as_jst_naive("2026-11-05T23:05:00+09:00")
      == datetime(2026, 11, 5, 23, 5))
check("ISO UTC is converted to JST",
      runner._as_jst_naive("2026-11-05T14:05:00+00:00")
      == datetime(2026, 11, 5, 23, 5))

# --- publish-time conflict guard -------------------------------------
runner.RUN_STARTED_AT = datetime(2026, 11, 5, 12, 0)
runner.write_heartbeat("checkpoint", "another runner")
try:
    runner._abort_if_another_writer_published()
    conflict_raised = False
except RuntimeError:
    conflict_raised = True
check("publish refuses to overwrite a mid-run publish", conflict_raised)
runner.RUN_STARTED_AT = datetime(2999, 1, 1)
check("publish proceeds when this run is the newest",
      runner._abort_if_another_writer_published() is None)

# --- heartbeat max_run_id: monotone, clock-independent ----------------
runner.RUN_STARTED_AT = datetime(2026, 11, 5, 12, 0)
# A remote DB that is strictly ahead: same lineage, one extra run.
src3 = SCRATCH / "src3.db"
shutil.copyfile(pulled, src3)          # the VM-adopted DB (1 ok row)
con = sqlite3.connect(src3)
con.execute(
    """INSERT INTO run_log (job, started_at, finished_at, status)
       VALUES ('ranking', '2026-11-05 23:20:00',
               '2026-11-05 23:21:00', 'ok')""")
con.commit()
con.close()
hb_local = runner.local_max_run_id()
storage.storage_put("estama.db", src3)   # remote now holds the newer id
fixture = {"job": "ranking", "finished_at": "2000-01-01 00:00:00 JST",
           "max_run_id": hb_local + 1, "snapshots": 2}
tmpf = SCRATCH / "hb-ahead.json"
tmpf.write_text(json.dumps(fixture))
storage.storage_put(runner.HEARTBEAT_KEY, tmpf)
check("id-ahead remote pulls despite an ancient stamp",
      runner._pull_if_remote_newer() is True)
check("after adopting it, the same heartbeat no longer pulls",
      runner._pull_if_remote_newer() is False)

# (b) an older remote id with a newer stamp still triggers the pull
# (fork case: two writers from the same base can hand out the same id)
fixture = {"job": "checkpoint", "finished_at": "2999-01-01 00:00:00 JST",
           "max_run_id": runner.local_max_run_id()}
tmpf.write_text(json.dumps(fixture))
storage.storage_put(runner.HEARTBEAT_KEY, tmpf)
check("legacy-style time rule still catches a fork",
      runner._pull_if_remote_newer() is True)

# (c) publish conflict is rejected by id alone, clock aside
runner.RUN_STARTED_AT = datetime(2999, 1, 1)  # time rule silent
fixture = {"job": "checkpoint", "finished_at": "2000-01-01 00:00:00 JST",
           "max_run_id": runner.local_max_run_id() + 999}
tmpf.write_text(json.dumps(fixture))
storage.storage_put(runner.HEARTBEAT_KEY, tmpf)
try:
    runner._abort_if_another_writer_published()
    id_conflict_raised = False
except RuntimeError:
    id_conflict_raised = True
check("publish conflict rejected by id alone", id_conflict_raised)
runner.RUN_STARTED_AT = datetime(2026, 11, 5, 12, 0)

# (d) legacy heartbeat (no max_run_id) falls back to the time rule
fixture = {"job": "checkpoint", "finished_at": "2999-01-01 00:00:00 JST"}
tmpf.write_text(json.dumps(fixture))
storage.storage_put(runner.HEARTBEAT_KEY, tmpf)
try:
    runner._abort_if_another_writer_published()
    legacy_conflict_raised = False
except RuntimeError:
    legacy_conflict_raised = True
check("heartbeat without max_run_id uses the time rule", legacy_conflict_raised)
runner.RUN_STARTED_AT = None

# (e) heartbeat round-trips max_run_id
runner.write_heartbeat("checkpoint", "id marker")
hb = runner.read_heartbeat()
check("heartbeat carries max_run_id", isinstance(hb.get("max_run_id"), int))
check("max_run_id matches the local DB",
      hb.get("max_run_id") == runner.local_max_run_id())
runner.write_heartbeat("checkpoint", "restore mine")
before = pulled.read_bytes()
STORE["estama.db.gz"] = b"definitely not gzip"
try:
    runner.job_pull()
    bad_gzip_raised = False
except IOError:
    bad_gzip_raised = True
check("pull fails on corrupt gzip", bad_gzip_raised)
check("bad download kept the previous DB", pulled.read_bytes() == before)
check("no .download residue",
      not (pulled.parent / "estama.db.download").exists()
      and not list(pulled.parent.glob("estama.db.download*")))

# valid gzip, but not a SQLite file: verify_db must reject it
STORE["estama.db.gz"] = gzip.compress(b"not a database", mtime=0)
try:
    runner.job_pull()
    bad_db_raised = False
except Exception:
    bad_db_raised = True
check("pull rejects a payload that is not a SQLite DB", bad_db_raised)
check("bad DB payload kept the previous DB", pulled.read_bytes() == before)

# ------------------------------------------------ concurrent upload safety
# Two writers must not interleave; the runner serialises with flock and
# the workflow with a concurrency group. Here we only prove the flock
# rejects a second local process.
import fcntl  # noqa: E402

lock = open(runner.LOCK_FILE, "w")
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
try:
    second = open(runner.LOCK_FILE, "w")
    try:
        fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
        blocked = False
    except BlockingIOError:
        blocked = True
    second.close()
finally:
    lock.close()
check("second writer is blocked by the runner lock", blocked)

srv.shutdown()
print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if fails else 0)
