"""Tests for runner.py's GitHub storage helpers (offline parts only):
key->path mapping, git blob SHA, and gzip payload determinism.
Run: python3 tests/test_gh_storage.py
"""
import gzip
import importlib.util
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["ESTAMA_VM_BASE"] = tempfile.mkdtemp(prefix="estama-gh-test-")

spec = importlib.util.spec_from_file_location("runner", ROOT / "runner.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

passed = 0


def check(name, cond):
    global passed
    assert cond, f"FAILED: {name}"
    passed += 1
    print(f"ok: {name}")


# key -> stored path mapping
check("live db gzipped", r.gh_path("estama.db") == "estama.db.gz")
check("archive db gzipped",
      r.gh_path("archive/estama-2026-10-26.db") == "archive/estama-2026-10-26.db.gz")
check("shops.json untouched", r.gh_path("shops.json") == "shops.json")
check("banned untouched", r.gh_path("banned_shops.json") == "banned_shops.json")

# git blob sha: the well-known vector for "hello\n"
check("blob sha vector",
      r.gh_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a")
check("blob sha empty", r.gh_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")

# payload: DB keys gzip (deterministic), JSON keys raw
tmp = Path(tempfile.mkdtemp())
f = tmp / "x.db"
f.write_bytes(b"SQLite format 3\0" + b"x" * 1000)
p1 = r.gh_payload(f, "estama.db")
p2 = r.gh_payload(f, "estama.db")
check("payload deterministic", p1 == p2)
check("payload is gzip", p1[:2] == b"\x1f\x8b")
check("payload roundtrip", gzip.decompress(p1) == f.read_bytes())
check("payload smaller", len(p1) < len(f.read_bytes()))

j = tmp / "shops.json"
j.write_bytes(b'{"shops": []}')
check("json payload raw", r.gh_payload(j, "shops.json") == b'{"shops": []}')

# backend selection: no storage.json in the temp base -> local mirror
check("default backend is local-mirror", r.backend_name() == "local-mirror")

print(f"ALL {passed} PASSED")
