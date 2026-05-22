"""L1 cache — Redis with a hard 60-second TTL.

Every key written here expires automatically; Redis is the authoritative
eviction boundary.  Connection errors are retried once then swallowed so a
Redis outage never propagates to the caller.
"""

import logging
from functools import lru_cache

import redis as _redis
from redis.exceptions import BusyLoadingError
from redis.exceptions import ConnectionError as RedisConnectionError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

_TTL_SECONDS = 60

_REDIS_RETRYABLE = (RedisConnectionError, BusyLoadingError)

# 2 attempts, 1–3 s backoff — fast is critical; cache misses fall through to L2.
_REDIS_RETRY = dict(
    retry=retry_if_exception_type(_REDIS_RETRYABLE),
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=3),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)


class RedisClient:
    """Wraps redis-py with documented retry on transient connection errors.

    Retried error types:
      ConnectionError  — TCP connection refused or dropped; transient.
      BusyLoadingError — Redis is loading the dataset after restart; transient.

    Not retried:
      TimeoutError        — socket timed out; fast-fail by design (2 s socket
                            timeout is intentional — cache misses fall through
                            to L2 immediately rather than stalling the pipeline).
      AuthenticationError — wrong password; permanent.
      ResponseError       — bad command / data type mismatch; permanent.

    All errors are swallowed after retries are exhausted — Redis is best-effort.
    GET failures return None (L2 fallback activates); SET failures are logged
    and silently skipped.
    """

    def __init__(self, url: str, decode_responses: bool = True) -> None:
        self._client = _redis.Redis.from_url(
            url,
            decode_responses=decode_responses,
            socket_connect_timeout=2,
            socket_timeout=2,
            retry_on_timeout=False,
        )

    @retry(**_REDIS_RETRY)
    def _get_raw(self, key: str):
        return self._client.get(key)

    @retry(**_REDIS_RETRY)
    def _set_raw(self, key: str, value, ex: int | None = None) -> None:
        self._client.set(key, value, ex=ex)

    def get(self, key: str):
        try:
            return self._get_raw(key)
        except Exception as exc:
            log.warning("Redis GET failed key=%r: %s", key, exc)
            return None

    def set(self, key: str, value, ex: int | None = None) -> None:
        try:
            self._set_raw(key, value, ex=ex)
        except Exception as exc:
            log.warning("Redis SET failed key=%r: %s", key, exc)


@lru_cache(maxsize=8)
def _client(url: str) -> RedisClient:
    return RedisClient(url, decode_responses=True)


def redis_get(url: str, key: str) -> str | None:
    return _client(url).get(key)


def redis_set(url: str, key: str, value: str) -> None:
    _client(url).set(key, value, ex=_TTL_SECONDS)
