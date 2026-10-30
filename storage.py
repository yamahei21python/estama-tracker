"""Object storage backends for runner.py.

Objects (the live DB, daily archives, dashboard, shops.json) are written
to exactly one backend, selected at call time:

- GitHub: when BASE/storage.json says {"backend": "github", ...}, files
  live on a dedicated `data` branch of the code repo (DB files
  gzip-compressed). Auth rides the custom.github-cli connector
  surrogate, so no token sits on the VM. Verification compares the
  remote blob SHA against the locally computed git blob SHA of the
  compressed bytes - stronger than a size check.
- Cloudflare R2 via boto3 when ESTAMA_R2_* env vars are set.
- Otherwise a local mirror directory (BASE/r2_local/).

runner.py does the DB-side work (verify, snapshot, upload, archive);
this module only moves bytes and proves they arrived.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

BASE = Path(os.environ.get("ESTAMA_VM_BASE", Path(__file__).resolve().parent))
LOCAL_MIRROR = BASE / "r2_local"
STORAGE_CONFIG = BASE / "storage.json"

GH_CONNECTOR = "custom.github-cli"
GH_API = "https://api.github.com"
GH_CRED_LIB = "/opt/muse/skills/custom-connector-setup/bin"


# ------------------------------------------------------------------ R2 ----

def r2_configured() -> bool:
    return all(
        os.environ.get(k)
        for k in ("ESTAMA_R2_ENDPOINT", "ESTAMA_R2_BUCKET",
                  "ESTAMA_R2_ACCESS_KEY_ID", "ESTAMA_R2_SECRET_ACCESS_KEY")
    )


def _s3():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["ESTAMA_R2_ENDPOINT"],
        aws_access_key_id=os.environ["ESTAMA_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["ESTAMA_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


# ------------------------------------------------------------- backend ----

def storage_config() -> dict:
    if STORAGE_CONFIG.exists():
        return json.loads(STORAGE_CONFIG.read_text())
    return {}


def backend_name() -> str:
    if storage_config().get("backend") == "github":
        return "github"
    if r2_configured():
        return "R2"
    return "local-mirror"


# ------------------------------------------------------- GitHub backend ----

def gh_path(key: str) -> str:
    if key.endswith(".db"):
        return key + ".gz"
    return key


def gh_blob_sha(data: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def gh_payload(local_file: Path, key: str) -> bytes:
    data = local_file.read_bytes()
    if gh_path(key) != key:
        data = gzip.compress(data, mtime=0)
    return data


def _gh_api(method: str, path: str, body: dict | None = None,
            timeout: int = 180) -> dict:
    sys.path.insert(0, GH_CRED_LIB)
    from dynamic_credentials import add_surrogate_to_request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GH_API + path, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    add_surrogate_to_request(req, GH_CONNECTOR, allowed_hosts=["api.github.com"])
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def _gh_repo_branch() -> tuple[str, str]:
    cfg = storage_config()
    return cfg.get("repo", "yamahei21python/estama-tracker"), cfg.get("branch", "data")


def _gh_head() -> tuple[str, str]:
    repo, branch = _gh_repo_branch()
    ref = _gh_api("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    commit_sha = ref["object"]["sha"]
    commit = _gh_api("GET", f"/repos/{repo}/git/commits/{commit_sha}")
    return commit_sha, commit["tree"]["sha"]


def _gh_entries() -> dict:
    """path -> tree entry for every blob on the data branch."""
    repo, _ = _gh_repo_branch()
    _, tree_sha = _gh_head()
    out = _gh_api("GET", f"/repos/{repo}/git/trees/{tree_sha}?recursive=1")
    return {e["path"]: e for e in out.get("tree", []) if e["type"] == "blob"}


def _gh_commit(entries: list[dict], message: str) -> None:
    repo, branch = _gh_repo_branch()
    head_sha, tree_sha = _gh_head()
    tree = _gh_api("POST", f"/repos/{repo}/git/trees",
                   {"base_tree": tree_sha, "tree": entries})
    commit = _gh_api("POST", f"/repos/{repo}/git/commits",
                     {"message": message, "tree": tree["sha"],
                      "parents": [head_sha]})
    _gh_api("PATCH", f"/repos/{repo}/git/refs/heads/{branch}",
            {"sha": commit["sha"]})


def _gh_put(key: str, local_file: Path) -> None:
    repo, _ = _gh_repo_branch()
    data = gh_payload(local_file, key)
    blob = _gh_api("POST", f"/repos/{repo}/git/blobs",
                   {"content": base64.b64encode(data).decode(),
                    "encoding": "base64"})
    if blob["sha"] != gh_blob_sha(data):
        raise IOError(f"github blob sha mismatch for {key}")
    _gh_commit([{"path": gh_path(key), "mode": "100644", "type": "blob",
                 "sha": blob["sha"]}], f"data: update {key}")


def _gh_size(key: str) -> int | None:
    entry = _gh_entries().get(gh_path(key))
    return int(entry["size"]) if entry else None


def _gh_verify(key: str, local_file: Path) -> bool:
    entry = _gh_entries().get(gh_path(key))
    return bool(entry) and entry["sha"] == gh_blob_sha(gh_payload(local_file, key))


def gh_live_blob(key: str) -> str | None:
    """Blob SHA currently stored at key (None when missing).

    API errors propagate on purpose: upload_db must abort before
    writing when it cannot capture a rollback target."""
    entry = _gh_entries().get(gh_path(key))
    return entry["sha"] if entry else None


def gh_restore(key: str, blob_sha: str) -> None:
    """Point key back at a previously verified blob in one commit."""
    _gh_commit([{"path": gh_path(key), "mode": "100644", "type": "blob",
                 "sha": blob_sha}],
               f"data: restore {key} (post-write verify failed)")


def _gh_delete(key: str) -> None:
    _gh_commit([{"path": gh_path(key), "mode": "100644", "type": "blob",
                 "sha": None}], f"data: delete {key}")


# ------------------------------------------------------------ dispatch ----

def storage_put(key: str, local_file: Path) -> None:
    b = backend_name()
    if b == "github":
        _gh_put(key, local_file)
    elif b == "R2":
        _s3().upload_file(str(local_file), os.environ["ESTAMA_R2_BUCKET"], key)
    else:
        dest = LOCAL_MIRROR / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_file, dest)


def storage_size(key: str) -> int | None:
    b = backend_name()
    if b == "github":
        try:
            return _gh_size(key)
        except Exception:
            return None
    if b == "R2":
        try:
            head = _s3().head_object(Bucket=os.environ["ESTAMA_R2_BUCKET"], Key=key)
            return int(head["ContentLength"])
        except Exception:
            return None
    p = LOCAL_MIRROR / key
    return p.stat().st_size if p.exists() else None


def storage_verify(key: str, local_file: Path) -> bool:
    """True when the stored object provably matches the local file."""
    if backend_name() == "github":
        try:
            return _gh_verify(key, local_file)
        except Exception:
            return False
    return storage_size(key) == local_file.stat().st_size


def storage_delete(key: str) -> None:
    b = backend_name()
    if b == "github":
        _gh_delete(key)
    elif b == "R2":
        _s3().delete_object(Bucket=os.environ["ESTAMA_R2_BUCKET"], Key=key)
    else:
        (LOCAL_MIRROR / key).unlink(missing_ok=True)


def storage_exists(key: str) -> bool:
    return storage_size(key) is not None


def storage_list(prefix: str = "") -> list[str]:
    """Every stored key under prefix, sorted."""
    b = backend_name()
    if b == "github":
        return sorted(k for k in _gh_entries() if k.startswith(prefix))
    if b == "R2":
        keys = []
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(
                Bucket=os.environ["ESTAMA_R2_BUCKET"], Prefix=prefix):
            keys.extend(o["Key"] for o in page.get("Contents", []))
        return sorted(keys)
    if not LOCAL_MIRROR.exists():
        return []
    return sorted(
        str(p.relative_to(LOCAL_MIRROR)) for p in LOCAL_MIRROR.rglob("*")
        if p.is_file() and str(p.relative_to(LOCAL_MIRROR)).startswith(prefix)
    )
