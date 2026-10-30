"""Tests for scripts/oneoff/gemini_retry_guard.py (archived, unused).

Run: python3 scripts/oneoff/test_gemini_retry.py
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent

# inject gemini_retry_guard as a module (no pip install needed)
spec = importlib.util.spec_from_file_location(
    "gemini_retry_guard", HERE / "gemini_retry_guard.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
retry_guard = mod.retry_guard
GeminiRetryError = mod.GeminiRetryError

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


# ---------- helper: mock function that raises ----------

def _make_failing(count, exc_cls=ConnectionError):
    """count 回 failure → (count+1) 回目以降 success を返す関数。"""
    n = [0]

    def fn():
        n[0] += 1
        if n[0] <= count:
            raise exc_cls("fail")
        return f"ok-{n[0]}"

    return fn, n


def _make_status_failing(status_code, count):
    """count 回 HTTPStatusRetry → 成功。"""
    n = [0]

    def fn():
        n[0] += 1
        if n[0] <= count:
            resp = MagicMock()
            resp.status_code = status_code
            raise mod.HTTPStatusRetry(status_code, resp)
        return "ok"

    return fn, n


# ---- test 1: connection error → retry → success ----

fn, counter = _make_failing(2, ConnectionError)
wrapped = retry_guard(max_retries=3, base_delay=0.001, jitter=0)(fn)
result = wrapped()
check("retry on ConnectionError, final ok", result == "ok-3")
check("retry count = 3 (1 initial + 2 retries)", counter[0] == 3)


# ---- test 2: 403 unrecoverable ----

fn2, _ = _make_failing(1, GeminiRetryError)
wrapped2 = retry_guard(max_retries=3, base_delay=0.001, jitter=0)(fn2)
try:
    wrapped2()
    check("GeminiRetryError raises immediately", False)
except GeminiRetryError:
    check("GeminiRetryError raises immediately", True)
except Exception:
    check("GeminiRetryError raises immediately", False)


# ---- test 3: max retries exhausted ----

fn3, counter3 = _make_failing(5, TimeoutError)
wrapped3 = retry_guard(max_retries=2, base_delay=0.001, jitter=0)(fn3)
try:
    wrapped3()
    check("exhausted retries → RuntimeError", False)
except RuntimeError as e:
    check("exhausted retries → RuntimeError", "2 retries" in str(e))
except Exception as e:
    check("exhausted retries → RuntimeError", False)
check("exhausted: attempted 3 times", counter3[0] == 3)


# ---- test 4: HTTP 503 retryable ----

fn4, counter4 = _make_status_failing(503, 2)
wrapped4 = retry_guard(max_retries=3, base_delay=0.001, jitter=0,
                       retryable_status={500, 502, 503, 504})(fn4)
result4 = wrapped4()
check("HTTP 503 retry → success", result4 == "ok")
check("HTTP 503 retry count = 3", counter4[0] == 3)


# ---- test 5: HTTP 429 NOT retryable (not in set) → immediate fail ----

fn5, counter5 = _make_status_failing(429, 2)
wrapped5 = retry_guard(max_retries=3, base_delay=0.001, jitter=0,
                       retryable_status={500, 502, 503, 504})(fn5)
try:
    wrapped5()
    check("HTTP 429 not in set → immediate raise", False)
except mod.HTTPStatusRetry:
    check("HTTP 429 not in set → immediate raise", True)
check("HTTP 429: only 1 attempt (no retry)", counter5[0] == 1)


# ---- test 6: immediate success (no retry) ----

fn6, counter6 = _make_failing(0, OSError)
wrapped6 = retry_guard(max_retries=3, base_delay=0.001, jitter=0)(fn6)
r6 = wrapped6()
check("immediate success, no retry", r6 == "ok-1")
check("counter = 1", counter6[0] == 1)


# ---- test 7: other exception (non-retryable) → immediate raise ----

fn7, counter7 = _make_failing(1, ValueError)
wrapped7 = retry_guard(max_retries=3, base_delay=0.001, jitter=0)(fn7)
try:
    wrapped7()
    check("ValueError immediate raise", False)
except ValueError:
    check("ValueError immediate raise", True)
check("ValueError: only 1 attempt", counter7[0] == 1)


# ---- test 8: backoff delay present (timing check) ----

import time

fn8, counter8 = _make_failing(3, ConnectionError)
wrapped8 = retry_guard(max_retries=3, base_delay=0.01, jitter=0,
                       factor=2.0)(fn8)
t0 = time.monotonic()
try:
    wrapped8()
except RuntimeError:
    pass
elapsed = time.monotonic() - t0
# expected: 0.01 + 0.02 + 0.04 = 0.07s minimum
check(f"backoff delay > 0.05s (actual {elapsed:.3f}s)", elapsed > 0.05)


# ---- test 9: jitter adds randomness ----

delays = []
def mock_sleep(d):
    delays.append(d)

fn9, _ = _make_failing(3, ConnectionError)
wrapped9 = retry_guard(max_retries=3, base_delay=0.01, jitter=0.005,
                       factor=2.0)(fn9)
with patch("time.sleep", mock_sleep):
    try:
        wrapped9()
    except RuntimeError:
        pass

check("jitter: 3 sleep calls", len(delays) == 3)
check("jitter: each has some variance", len(set(delays)) > 1)


# ---- test 10: retryable_status=None → status code ignored, no retry ----

fn10, counter10 = _make_status_failing(404, 2)
wrapped10 = retry_guard(max_retries=3, base_delay=0.001, jitter=0)(fn10)
try:
    wrapped10()
    check("retryable_status=None: 404 not retried", False)
except mod.HTTPStatusRetry:
    check("retryable_status=None: 404 not retried, immediate raise", True)
check("retryable_status=None: only 1 attempt", counter10[0] == 1)


# ---- test 11: @retry_guard preserves function name ----

@retry_guard(max_retries=1, base_delay=0.001, jitter=0)
def named_func():
    return "hello"

check("wraps preserves __name__", named_func.__name__ == "named_func")


# ---- test 12: response returned intact on success ----

resp_mock = MagicMock()
resp_mock.status_code = 200
resp_mock.json.return_value = {"key": "val"}
resp_mock.text = "body"

fn12, _ = _make_failing(0, Exception)
wrapped12 = retry_guard(max_retries=3, base_delay=0.001, jitter=0)(fn12)
# overwrite to return a mock
original_fn = fn12
def fn12_ok():
    return resp_mock
wrapped12_ok = retry_guard(max_retries=3, base_delay=0.001, jitter=0)(fn12_ok)
r = wrapped12_ok()
check("response returned intact", r is resp_mock)
check("response.json() works", r.json() == {"key": "val"})


print()
print("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)