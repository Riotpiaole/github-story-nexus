"""L1 cache — Redis with a hard 60-second TTL.

Every key written here expires automatically; Redis is the authoritative
eviction boundary.  Connection errors are caught and logged so a Redis
outage never propagates to the caller.
"""

import logging
from functools import lru_cache

import redis as _redis

log = logging.getLogger(__name__)

_TTL_SECONDS = 60


@lru_cache(maxsize=8)
def _client(url: str) -> _redis.Redis:
    return _redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        retry_on_timeout=False,
    )


def redis_get(url: str, key: str) -> str | None:
    try:
        return _client(url).get(key)
    except Exception as exc:
        log.warning("Redis GET failed key=%r: %s", key, exc)
        return None


def redis_set(url: str, key: str, value: str) -> None:
    try:
        _client(url).set(key, value, ex=_TTL_SECONDS)
    except Exception as exc:
        log.warning("Redis SET failed key=%r: %s", key, exc)
