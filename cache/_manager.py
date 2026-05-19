"""Two-level cache manager.

Read path:
  1. Redis GET  — hit → return value (L1 warm)
  2. PG SELECT  — hit → backfill Redis, return value (L1 cold, L2 warm)
  3. Both miss  → return None (caller must compute and call set())

Write path:
  Redis SET (EX 60) then PG UPSERT, both fire-and-forget on error.
"""

import logging

from ._pg import pg_get, pg_set
from ._redis import redis_get, redis_set

log = logging.getLogger(__name__)


class CacheManager:
    """L1 (Redis, 60 s TTL) → L2 (PostgreSQL, persistent) layered KV cache."""

    def __init__(self, redis_url: str, pg_dsn: str) -> None:
        self._redis_url = redis_url
        self._pg_dsn = pg_dsn

    def get(self, key: str) -> str | None:
        value = redis_get(self._redis_url, key)
        if value is not None:
            log.debug("cache L1 hit key=%r", key)
            return value

        log.debug("cache L1 miss key=%r — checking L2", key)
        value = pg_get(self._pg_dsn, key)
        if value is not None:
            log.debug("cache L2 hit key=%r — backfilling L1", key)
            redis_set(self._redis_url, key, value)
            return value

        log.debug("cache miss (both layers) key=%r", key)
        return None

    def set(self, key: str, value: str) -> None:
        redis_set(self._redis_url, key, value)
        pg_set(self._pg_dsn, key, value)
