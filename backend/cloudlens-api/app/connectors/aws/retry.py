"""Retry / backoff helpers for transient AWS API failures."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from botocore.exceptions import BotoCoreError, ClientError

T = TypeVar("T")

# Error codes that are safe to retry for STS / identity calls.
_RETRYABLE_CLIENT_CODES = frozenset(
    {
        "RequestTimeout",
        "RequestTimeoutException",
        "PriorRequestNotComplete",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
        "ServiceUnavailable",
        "InternalError",
        "InternalFailure",
        "ServiceFailure",
    }
)


def is_retryable_aws_error(exc: BaseException) -> bool:
    if isinstance(exc, BotoCoreError) and not isinstance(exc, ClientError):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in _RETRYABLE_CLIENT_CODES:
            return True
        if isinstance(http_status, int) and http_status in {429, 500, 502, 503, 504}:
            return True
    return False


def call_with_backoff(
    operation: Callable[[], T],
    *,
    max_attempts: int = 5,
    base_delay_seconds: float = 0.2,
    max_delay_seconds: float = 8.0,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
    is_retryable: Callable[[BaseException], bool] | None = None,
) -> T:
    """Execute ``operation`` with exponential backoff and full jitter."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    retry_check = is_retryable or is_retryable_aws_error
    attempt = 1
    while True:
        try:
            return operation()
        except Exception as exc:
            if attempt >= max_attempts or not retry_check(exc):
                raise
            # Full jitter: delay in [0, min(cap, base * 2^(attempt-1))]
            ceiling = min(max_delay_seconds, base_delay_seconds * (2 ** (attempt - 1)))
            sleep(ceiling * rng())
            attempt += 1
