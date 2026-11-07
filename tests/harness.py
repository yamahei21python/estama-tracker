"""Shared assertion harness for the standalone test scripts.

Historically every test_*.py copy-pasted the same four lines (`fails`,
`check`, the banner print). They all did the same three things:
record a failure, print PASS/FAIL per assertion, exit non-zero if any
failed. Centralising removes the copies and gives pytest and the CI a
single place to hook into.

Usage per test file:

    from harness import check, fails
    ...                      # test body
    print("\\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
    sys.exit(1 if fails else 0)

Tests run as scripts (`python3 tests/test_x.py`) and import this module
from tests/ (the script directory is on sys.path).
"""
fails: list[str] = []


def check(name: str, cond, extra: str = "") -> None:
    if cond:
        print("PASS", name)
        return
    print("FAIL", name)
    fails.append(name)
    if extra:
        print(extra)
        fails.append(extra)
