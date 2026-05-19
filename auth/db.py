"""MongoDB client with connection pooling, hard timeouts, and tenacity retry."""

import logging
import os
from functools import lru_cache

from pymongo import ASCENDING, MongoClient
from pymongo.errors import (
    ConnectionFailure,
    NetworkTimeout,
    ServerSelectionTimeoutError,
)
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

_DB_NAME = "story_pr_agent"
_DEFAULT_URI: str = os.environ.get("MONGODB_URI", "")

# How long a single socket op may block before raising NetworkTimeout
_SOCKET_TIMEOUT_MS = 10_000
# How long the driver waits to establish a TCP connection
_CONNECT_TIMEOUT_MS = 5_000
# How long find_one / insert / etc. may wait for a suitable server
_SERVER_SELECTION_TIMEOUT_MS = 10_000
_MAX_POOL_SIZE = 20
_MIN_POOL_SIZE = 2

_RETRYABLE_ERRORS = (ConnectionFailure, ServerSelectionTimeoutError, NetworkTimeout)


@retry(
    retry=retry_if_exception_type(_RETRYABLE_ERRORS),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def _ping(client: MongoClient) -> None:
    """Verify the server is reachable; retried by tenacity on transient failures."""
    client.admin.command("ping")


def _build_client(uri: str) -> MongoClient:
    """Create and verify a MongoClient.  Raises on permanent connection failure."""
    client: MongoClient = MongoClient(
        uri,
        connectTimeoutMS=_CONNECT_TIMEOUT_MS,
        socketTimeoutMS=_SOCKET_TIMEOUT_MS,
        serverSelectionTimeoutMS=_SERVER_SELECTION_TIMEOUT_MS,
        maxPoolSize=_MAX_POOL_SIZE,
        minPoolSize=_MIN_POOL_SIZE,
        retryWrites=True,
        retryReads=True,
    )
    _ping(client)
    log.info("MongoDB connection established.")
    return client


@lru_cache(maxsize=1)
def get_client(uri: str = _DEFAULT_URI) -> MongoClient:
    """Return a process-wide MongoClient (created once, reused across requests).

    Defaults to the MONGODB_URI environment variable so callers inside Docker
    need not pass a URI explicitly.
    """
    return _build_client(uri)


def get_db(uri: str = _DEFAULT_URI):
    """Return the application database handle.

    URI resolution order:
      1. Explicit argument (used when Settings overrides the default).
      2. MONGODB_URI environment variable (set by docker-compose for the app container).
      3. mongodb://localhost:27017 (local dev fallback).
    """
    return get_client(uri)[_DB_NAME]


def ensure_indexes(db) -> None:
    """Create indexes required for auth queries (idempotent).

    (provider, provider_id) is the natural unique key — one user doc per
    provider account.  email is indexed for lookup but not unique because the
    same email address may exist across multiple providers.
    """
    db.users.create_index(
        [("provider", ASCENDING), ("provider_id", ASCENDING)],
        unique=True,
        background=True,
    )
    db.users.create_index([("email", ASCENDING)], background=True)
