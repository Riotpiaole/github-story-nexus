"""L2 cache — PostgreSQL persistent store.

Rows live indefinitely; Redis (L1) is the TTL boundary.  This layer exists
so that a Redis eviction or restart doesn't force a full re-computation —
the value can be backfilled from here.

The table is bootstrapped idempotently on first use via DDL run inside
_ensure_table().  psycopg_pool.ConnectionPool is thread-safe and reuses
connections across concurrent requests.
"""

import logging
from functools import lru_cache

from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS context_cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


@lru_cache(maxsize=8)
def _pool(dsn: str) -> ConnectionPool:
    pool = ConnectionPool(dsn, min_size=1, max_size=5, open=True)
    with pool.connection() as conn:
        conn.execute(_DDL)
    return pool


def pg_get(dsn: str, key: str) -> str | None:
    try:
        with _pool(dsn).connection() as conn:
            row = conn.execute(
                "SELECT value FROM context_cache WHERE key = %s", (key,)
            ).fetchone()
        return row[0] if row else None
    except Exception as exc:
        log.warning("PG GET failed key=%r: %s", key, exc)
        return None


def pg_set(dsn: str, key: str, value: str) -> None:
    try:
        with _pool(dsn).connection() as conn:
            conn.execute(
                """
                INSERT INTO context_cache (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        created_at = NOW()
                """,
                (key, value),
            )
    except Exception as exc:
        log.warning("PG SET failed key=%r: %s", key, exc)
