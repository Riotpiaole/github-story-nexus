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

_SOCKET_TIMEOUT_MS = 10_000
_CONNECT_TIMEOUT_MS = 5_000
_SERVER_SELECTION_TIMEOUT_MS = 10_000
_MAX_POOL_SIZE = 20
_MIN_POOL_SIZE = 2

_RETRYABLE_ERRORS = (ConnectionFailure, ServerSelectionTimeoutError, NetworkTimeout)


class MongoDBClient:
    """Wraps PyMongo with retry on transient connection errors.

    Retried error types (not HTTP status codes — MongoDB uses error classes):
      ConnectionFailure           — TCP connection to mongod dropped; transient.
      ServerSelectionTimeoutError — No primary found within timeout; transient
                                    (replica set failover or restart in progress).
      NetworkTimeout              — Socket read/write timed out mid-operation;
                                    transient network blip.

    Not retried:
      OperationFailure (write error) — document validation, duplicate key, etc.
      InvalidId                      — malformed ObjectId; permanent.
      ConfigurationError             — misconfigured URI; permanent.

    Retry policy: 5 attempts, exponential backoff 1–30 s.
    """

    def __init__(self, uri: str = _DEFAULT_URI) -> None:
        self._uri = uri
        self._client: MongoClient | None = None

    @retry(
        retry=retry_if_exception_type(_RETRYABLE_ERRORS),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
    def _ping(self, client: MongoClient) -> None:
        """Verifies the server is reachable; retried on transient failures."""
        client.admin.command("ping")

    def _build_client(self) -> MongoClient:
        """Creates and verifies a MongoClient. Raises on permanent connection failure."""
        client: MongoClient = MongoClient(
            self._uri,
            connectTimeoutMS=_CONNECT_TIMEOUT_MS,
            socketTimeoutMS=_SOCKET_TIMEOUT_MS,
            serverSelectionTimeoutMS=_SERVER_SELECTION_TIMEOUT_MS,
            maxPoolSize=_MAX_POOL_SIZE,
            minPoolSize=_MIN_POOL_SIZE,
            retryWrites=True,
            retryReads=True,
        )
        self._ping(client)
        log.info("MongoDB connection established.")
        return client

    def get_client(self) -> MongoClient:
        """Returns the process-wide MongoClient (created once, reused across requests)."""
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def get_db(self):
        """Returns the application database handle."""
        return self.get_client()[_DB_NAME]

    def ensure_indexes(self, db) -> None:
        """Creates indexes required for auth queries (idempotent).

        (provider, provider_id) is the natural unique key — one user doc per
        provider account. email is indexed for lookup but not unique because the
        same email address may exist across multiple providers.
        """
        db.users.create_index(
            [("provider", ASCENDING), ("provider_id", ASCENDING)],
            unique=True,
            background=True,
        )
        db.users.create_index([("email", ASCENDING)], background=True)


# Process-wide singleton (URI set at startup via _DEFAULT_URI / Settings override)
@lru_cache(maxsize=1)
def _get_mongo_client(uri: str = _DEFAULT_URI) -> MongoDBClient:
    return MongoDBClient(uri)


# Backward-compat module-level API used by auth/__init__.py
def get_db(uri: str = _DEFAULT_URI):
    """Returns the application database handle."""
    return _get_mongo_client(uri).get_db()


def ensure_indexes(db) -> None:
    """Creates indexes required for auth queries (idempotent)."""
    _get_mongo_client().ensure_indexes(db)
