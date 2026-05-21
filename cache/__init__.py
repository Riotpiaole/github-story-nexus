"""Context cache package.

Public API
----------
get_cache() -> CacheManager
    Returns the process-wide singleton for the context cache (Redis + PG).
    Connections are lazy — no network traffic until the first get() / set() call.

llm_cache
---------
Two-level LLM prompt-response cache (LocalCache L1 + Redis L2).

    from cache import get_local_cache, make_cache_key, llm_redis_get, llm_redis_set

    local = get_local_cache()            # singleton per process
    key, compressed = make_cache_key(prompt)
    response = local.get(key, compressed)
    if response is None:
        response = llm_redis_get(redis_url, key, compressed)
        ...

Usage
-----
    from cache import get_cache

    cache = get_cache()
    value = cache.get(key)          # str | None
    if value is None:
        value = expensive_compute()
        cache.set(key, value)
"""

from functools import lru_cache

from ._llm import LocalCache, get_local_cache, make_cache_key
from ._llm import redis_get as llm_redis_get
from ._llm import redis_set as llm_redis_set
from ._manager import CacheManager

__all__ = [
    "CacheManager",
    "get_cache",
    "LocalCache",
    "get_local_cache",
    "make_cache_key",
    "llm_redis_get",
    "llm_redis_set",
]


@lru_cache(maxsize=1)
def get_cache() -> CacheManager:
    from config import get_settings
    s = get_settings()
    return CacheManager(redis_url=s.redis_url, pg_dsn=s.postgres_vec_url)
