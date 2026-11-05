"""Repo-wide policy defaults - the single source of truth.

`storage.json` stays the place for host-specific connection details and
for a host-local override of a single policy value. Anything that must
not drift between the VM and GitHub Actions belongs in
`config/policy.json` (which the VM clones and which CI audits can see).

Deletion is opt-in by design, so an unreadable policy disables every
policy default instead of falling back to partial values: silence must
mean "do not delete".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = REPO_ROOT / "config" / "policy.json"


def _policy_path() -> Path:
    import os

    override = os.environ.get("ESTAMA_POLICY_PATH")
    return Path(override) if override else DEFAULT_POLICY


def load_policy() -> dict:
    """Return the policy file as a dict ({} when missing or unreadable)."""
    path = _policy_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # ValueError covers json.JSONDecodeError. A corrupt policy
        # pretends to be no policy at all: nothing is pruned.
        sys.stderr.write(f"policy: ignoring {path} ({exc}); "
                         f"retention defaults disabled\n")
        return {}
    return raw if isinstance(raw, dict) else {}


def db_retention_config(storage_override) -> dict | None:
    """Policy defaults for `db_retention`, with storage.json on top.

    Returns None when nothing is configured anywhere (the pre-policy
    behaviour: no key -> no deletion). Per-field semantics (0/None
    disables a table, a malformed value disables just that table) are
    left to `retention.retention_config`, which works on this dict the
    same way it used to work on storage.json's raw section.
    """
    merged: dict = {}
    base = load_policy().get("db_retention")
    if isinstance(base, dict):
        merged.update(base)
    if isinstance(storage_override, dict):
        merged.update(storage_override)
    return merged or None


def archive_retention_config(storage_override) -> dict:
    """Same merge for `archive_retention`.

    The policy deliberately ships no archive window: deleting raw
    archives must stay an explicit choice made in the host's
    storage.json.
    """
    merged: dict = {}
    base = load_policy().get("archive_retention")
    if isinstance(base, dict):
        merged.update(base)
    if isinstance(storage_override, dict):
        merged.update(storage_override)
    return merged
