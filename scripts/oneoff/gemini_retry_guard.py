"""gemini_retry_guard — HTTP リクエスト保護のretryデコレータ。

Exponential-backoff + jitter + status-code aware retry。
scraper の POST/GET を一箇所から防御する。

設計方針:
- UnrecoverableError (GeminiRetryError) を raise すると即fail (retryしない)。
- 5xx / timeout / ConnectionError は retry対象。
- backoff = base * (factor ** attempt) + jitter。
- logger は logging モジュール (レベル: retry=INFO, final=ERROR)。

Usage:
    @retry_guard(max_retries=3, base_delay=0.5,
                 retryable_status={500, 502, 503, 504})
    def my_http_get(url):
        return session.get(url)
"""

from __future__ import annotations

import logging
import random
import time
from functools import wraps
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from requests import Response

logger = logging.getLogger("gemini.retry")


class GeminiRetryError(Exception):
    """このエラー = 再試行しない (unrecoverable)。

    例: 403, 429, 404 等の「待っても変わらない」ステータス。
    """


def _backoff_delay(attempt: int, base: float, factor: float, jitter: float) -> float:
    return base * (factor ** attempt) + random.uniform(0, jitter)


def retry_guard(
    max_retries: int = 3,
    base_delay: float = 0.5,
    factor: float = 2.0,
    jitter: float = 0.3,
    retryable_status: set[int] | None = None,
) -> callable:
    """関数に retry を貼り付けるデコレータ。

    Args:
        max_retries: 最大再試行回数 (0 = 1回のみ実行)。
        base_delay: 初期待機秒。
        factor: 指数バックオフの基底。
        jitter: ランダムずれの最大値。
        retryable_status: retry対象のHTTPステータスコードのset。
                          None の場合は空set (statusコードは検査しない)。
    """
    if retryable_status is None:
        retryable_status = set()

    def decorator(func: callable) -> callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Response:
            last_exc: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    result = func(*args, **kwargs)
                    # status-code 検査 (response に status_code 属性があれば)
                    sc = getattr(result, "status_code", None)
                    if sc is not None and sc in retryable_status:
                        raise HTTPStatusRetry(sc, result)
                    return result
                except GeminiRetryError:
                    raise  # unrecoverable → 即fail
                except HTTPStatusRetry as exc:
                    if exc.status_code in retryable_status:
                        last_exc = exc
                    else:
                        logger.error("%s unrecoverable HTTP %d",
                                     func.__name__, exc.status_code)
                        raise
                except (TimeoutError, OSError, ConnectionError) as exc:
                    last_exc = exc
                except Exception as exc:
                    # 他の例外は即fail (unrecoverable)
                    logger.error("%s unrecoverable: %s", func.__name__, exc)
                    raise

                if attempt < max_retries:
                    delay = _backoff_delay(attempt, base_delay, factor, jitter)
                    logger.info(
                        "%s attempt %d/%d failed (%s), retry in %.2fs",
                        func.__name__, attempt + 1, max_retries + 1,
                        type(last_exc).__name__, delay,
                    )
                    time.sleep(delay)

            logger.error(
                "%s exhausted %d retries, last: %s",
                func.__name__, max_retries, last_exc,
            )
            raise RuntimeError(
                f"{func.__name__} failed after {max_retries} retries"
            ) from last_exc

        return wrapper
    return decorator


class HTTPStatusRetry(Exception):
    """内部例外: status_code が retryable_status に該当したことを通知。"""

    def __init__(self, status_code: int, response):
        self.status_code = status_code
        self.response = response
        super().__init__(f"HTTP {status_code}")