from functools import lru_cache

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool


@lru_cache(maxsize=1)
def get_checkpointer(dsn: str) -> PostgresSaver:
    """Returns a PostgresSaver backed by a dedicated connection pool.

    autocommit=True and prepare_threshold=0 are required by langgraph-checkpoint-postgres.
    This pool is kept separate from the one in cache/_pg.py (context_cache table) to
    avoid transaction conflicts.
    """
    pool = ConnectionPool(
        conninfo=dsn,
        max_size=5,
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=True,
    )
    checkpointer = PostgresSaver(pool)
    checkpointer.setup()  # idempotent — creates checkpoint tables on first call
    return checkpointer
