"""Tests for storage.py's Worker-gateway backend (offline parts):
backend selection, key->URL mapping, gzip payload handling, and the
put/size/verify/delete dispatch against an in-memory fake of the
gateway HTTP interface.

The fake follows the REAL Worker contract: HEAD returns X-Object-Size
and an ETag holding the stored object's MD5 (a Content-Length set by
the Worker itself would be ignored by the Workers runtime, so it is
never the source of truth here). .db keys are stored gzip-compressed
under a .gz suffix, exactly like the GitHub backend - the raw DB is
over the Workers 100 MB request limit. There is no storage_copy:
upload_db writes the live key directly and verifies it.

Run: python3 tests/test_gw_storage.py
"""
import gzip
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = Path(tempfile.mkdtemp(prefix="estama-gw-test-"))
os.environ["ESTAMA_VM_BASE"] = str(BASE)
(BASE / "storage.json").write_text(json.dumps({
    "backend": "gateway",
    "gateway_base": "https://estama-r2.example.workers.dev",
}))

spec = importlib.util.spec_from_file_location("storage", ROOT / "storage.py")
s = importlib.util.module_from_spec(spec)
sys.modules["storage"] = s
spec.loader.exec_module(s)

passed = 0


def check(name, cond):
    global passed
    assert cond, f"FAILED: {name}"
    passed += 1
    print(f"ok: {name}")


# backend selection + key/URL mapping
check("gateway backend selected", s.backend_name() == "gateway")
check("db key gains .gz", s.gw_path("estama.db") == "estama.db.gz")
check("non-db key unchanged", s.gw_path("shops.json") == "shops.json")
check("db key url",
      s._gw_url(s.gw_path("estama.db"))
      == "https://estama-r2.example.workers.dev/estama.db.gz")
check("archive key keeps slashes",
      s._gw_url(s.gw_path("archive/estama-2026-10-26.db"))
      == "https://estama-r2.example.workers.dev/archive/estama-2026-10-26.db.gz")
check("key quoting", s._gw_url("a b.json").endswith("/a%20b.json"))

# in-memory fake of the gateway: url -> bytes, HEAD per the real contract
store = {}
fail_put = {"on": False}
omit_size = {"on": False}


def fake_http(method, url, data=None, content_type=None, timeout=180):
    base = "https://estama-r2.example.workers.dev"
    assert url.startswith(base), url
    key = url[len(base):].lstrip("/")
    if method == "PUT":
        if fail_put["on"]:
            return 500, {}, b""
        store[key] = data
        return 200, {}, b""
    if method == "HEAD":
        if key not in store:
            return 404, {}, b""
        headers = {"ETag": f'"{hashlib.md5(store[key]).hexdigest()}"'}
        if not omit_size["on"]:
            headers["X-Object-Size"] = str(len(store[key]))
        return 200, headers, b""
    if method == "DELETE":
        store.pop(key, None)
        return 204, {}, b""
    raise AssertionError(f"unexpected {method} {url}")


s._gw_http = fake_http

tmp = Path(tempfile.mkdtemp())
f = tmp / "x.db"
f.write_bytes(b"SQLite format 3\0" + b"y" * 5000)
payload = gzip.compress(f.read_bytes(), mtime=0)

# direct live-key flow (what upload_db does: put -> verify)
s.storage_put("estama.db", f)
check("put stored the gz payload", store.get("estama.db.gz") == payload)
check("payload round-trips", gzip.decompress(store["estama.db.gz"]) == f.read_bytes())
check("size is the stored size", s.storage_size("estama.db") == len(payload))
check("exists", s.storage_exists("estama.db"))
check("verify by etag/md5", s.storage_verify("estama.db", f))

# a corrupted stored object must fail verification
store["estama.db.gz"] = store["estama.db.gz"][:-1]
check("verify false when stored object differs", not s.storage_verify("estama.db", f))
s.storage_put("estama.db", f)
check("verify true again after re-put", s.storage_verify("estama.db", f))

# HEAD without X-Object-Size: size is None, content is still provable
omit_size["on"] = True
check("size None when X-Object-Size missing", s.storage_size("estama.db") is None)
check("verify still true via etag", s.storage_verify("estama.db", f))
omit_size["on"] = False

s.storage_put("archive/estama-test.db", f)
check("archive key stored gz", store.get("archive/estama-test.db.gz") == payload)
s.storage_delete("archive/estama-test.db")
check("delete removed key", "archive/estama-test.db.gz" not in store)
check("size of missing is None", s.storage_size("archive/estama-test.db") is None)
check("missing does not exist", not s.storage_exists("archive/estama-test.db"))
check("verify false when missing", not s.storage_verify("archive/estama-test.db", f))

fail_put["on"] = True
try:
    s.storage_put("estama.db", f)
    check("failed put raises", False)
except IOError:
    check("failed put raises", True)

print(f"ALL {passed} PASSED")
