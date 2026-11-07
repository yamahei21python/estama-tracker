"""Backend classes and the facades over them.

The refactoring in this file is purely structural: each backend owns
its operation set, so the call site never re-implements the dispatch.
Behaviour is byte-identical to the pre-class code, with one deliberate
change: a backend that cannot list its keys raises NotImplementedError
instead of returning an empty listing (an empty list look like
"nothing is there", which silently disabled archive retention).

Run: python3 tests/test_backends.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="estama-backends-test-"))
os.environ["ESTAMA_VM_BASE"] = str(SCRATCH)
sys.path.insert(0, str(ROOT))

import storage  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


def use_backend(payload):
    (SCRATCH / "storage.json").write_text(json.dumps(payload), encoding="utf-8")


# selection ----------------------------------------------------------
use_backend({"backend": "github"})
check("backend_name agrees with get_backend().name",
      storage.backend_name() == storage.get_backend().name == "github")
check("get_backend returns a fresh backend each call",
      storage.get_backend() is not storage.get_backend())

use_backend({"backend": "gateway", "gateway_base": "https://x.workers.dev"})
check("gateway selected",
      isinstance(storage.get_backend(), storage.GatewayBackend)
      and storage.storage_supports_get())

use_backend({})
check("no backend -> local mirror, supports downloads",
      isinstance(storage.get_backend(), storage.LocalMirrorBackend)
      and storage.storage_supports_get())

os.environ.pop("ESTAMA_R2_ENDPOINT", None)
check("R2 path only when env vars are set",
      not isinstance(storage.get_backend(), storage.R2Backend))

# capability ----------------------------------------------------------
check("GitHub backend declares no download path",
      storage.GitHubBackend().supports_get is False)
check("R2 backend declares no download path",
      storage.R2Backend().supports_get is False)
try:
    storage.GitHubBackend().get("a", SCRATCH / "x.db")
    gh_get_raised = False
except NotImplementedError:
    gh_get_raised = True
check("GitHub gets raise NotImplementedError", gh_get_raised)

# the honest error instead of an empty listing ------------------------
use_backend({"backend": "gateway", "gateway_base": "https://x.workers.dev"})
try:
    storage.storage_list("archive/")
    gw_list_raised = False
except NotImplementedError as exc:
    gw_list_raised = "archive retention" in str(exc)
check("gateway storage_list raises a loud NotImplementedError", gw_list_raised)

# GitHub listing is prefix-filtered and sorted (mocked: the real one
# would call the GitHub API)
orig_entries = storage._gh_entries
storage._gh_entries = lambda: {"c/x": {}, "a/z": {}, "a/b": {}}
use_backend({"backend": "github"})
try:
    check("github list is prefix-filtered and sorted",
          storage.storage_list("a/") == ["a/b", "a/z"])
    check("github list carries every prefix",
          storage.storage_list("") == ["a/b", "a/z", "c/x"])
finally:
    storage._gh_entries = orig_entries

# local mirror round trip through the new classes ----------------------
use_backend({})
src = SCRATCH / "mirror.db"
src.write_bytes(b"mirror-bytes")
storage.storage_put("estama.db", src)
check("mirror write lands under the key",
      (SCRATCH / "r2_local" / "estama.db").read_bytes() == b"mirror-bytes")
dest = SCRATCH / "mirror-out.db"
check("mirror read returns True", storage.storage_get("estama.db", dest))
check("mirror read is byte-identical", dest.read_bytes() == b"mirror-bytes")
check("mirror list yields the key, sorted",
      storage.storage_list("") == ["estama.db"])
check("copy_object is gateway-only",
      not hasattr(storage.LocalMirrorBackend(), "copy_object"))

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
import shutil  # noqa: E402

shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if fails else 0)
