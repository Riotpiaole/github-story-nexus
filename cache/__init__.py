"""Context cache package.

Public API
----------
get_cache() -> CacheManager
    Returns the process-wide singleton.  Connections are lazy — no network
    traffic until the first get() / set() call.

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

from ._manager import CacheManager

__all__ = ["CacheManager", "get_cache"]


@lru_cache(maxsize=1)
def get_cache() -> CacheManager:
    from config import get_settings
    s = get_settings()
    return CacheManager(redis_url=s.redis_url, pg_dsn=s.postgres_vec_url)
