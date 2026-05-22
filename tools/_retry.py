import logging

from tenacity import (
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


class RetryableError(Exception):
    """Raised by client methods to signal a retryable condition.

    Attributes:
        status_code: HTTP status or exit code that triggered this error.
        reason: Human-readable explanation of WHY this code is retried.
    """

    def __init__(self, status_code: int | str, message: str, reason: str) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"[{status_code}] {message} — retryable: {reason}")


# GitHub REST API: 3 attempts, 2–30 s exponential backoff
GITHUB_RETRY = dict(
    retry=retry_if_exception_type(RetryableError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)

# Anthropic LLM API: 4 attempts, 5–60 s exponential backoff (heavier — model inference is slow)
LLM_RETRY = dict(
    retry=retry_if_exception_type(RetryableError),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=5, max=60),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)

# MongoDB: 5 attempts, 1–30 s exponential backoff (connection errors need more patience)
MONGO_RETRY = dict(
    retry=retry_if_exception_type(RetryableError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
