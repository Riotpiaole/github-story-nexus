import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import retry
from tools.langchain_tools import get_code_search_tools

from cache._llm import get_local_cache, make_cache_key
from cache._llm import redis_get as _llm_redis_get
from cache._llm import redis_set as _llm_redis_set
from tools._retry import LLM_RETRY, RetryableError


# Explicitly load .env into os.environ before Settings is instantiated.
# pydantic-settings also reads env_file directly, but calling load_dotenv()
# here ensures os.getenv() calls anywhere else in the process see the same values.
load_dotenv()


class Settings(BaseSettings):
    # ── Anthropic ──────────────────────────────────────────────────────────────
    anthropic_api_key: str

    # ── GitHub OAuth App ──────────────────────────────────────────────────────
    github_client: str = ""
    github_secret: str = ""

    # ── GitHub — PAT or App (at least one must be set) ─────────────────────────
    github_token: str = ""
    github_app_id: str = "3770889"
    github_private_key_path: str = ""
    github_installation_id: int = 0
    github_webhook_secret: str = ""

    # ── Agent tuning ───────────────────────────────────────────────────────────
    max_retries: int = 3
    llm_model: str = "claude-sonnet-4-6"
    base_branch: str = "main"

    # ── LangSmith tracing (optional) ───────────────────────────────────────────
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "story-pr-agent"

    # ── Cache infrastructure ────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379"
    postgres_vec_url: str = "postgresql://postgres:postgres@localhost:5432/vectordb"

    # ── MongoDB ────────────────────────────────────────────────────────────────
    mongo_username: str
    mongo_password: SecretStr
    mongodb_uri: SecretStr

    # ── Flask ──────────────────────────────────────────────────────────────────
    flask_secret_key: SecretStr

    # ── Google OAuth ───────────────────────────────────────────────────────────
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Loads and caches settings from .env file using Pydantic."""
    return Settings()


def _configure_langsmith(s: Settings) -> None:
    """Enables LangSmith tracing if configured in settings."""
    if s.langchain_tracing_v2:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = s.langchain_api_key
        os.environ["LANGCHAIN_PROJECT"] = s.langchain_project


def configure_logging() -> None:
    """Sets up basic logging with ISO-format timestamps."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


settings = get_settings()
_configure_langsmith(settings)
configure_logging()

log = logging.getLogger(__name__)

_AGENT_MAX_TOKENS = 5000


def _messages_to_str(messages: list) -> str:
    return "\n---\n".join(
        f"{getattr(m, 'type', type(m).__name__)}:{m.content if isinstance(m.content, str) else str(m.content)}"
        for m in messages
    )


_LLM_RETRY_REASONS: dict[int, str] = {
    429: "Rate limited: Anthropic throttles excessive requests; back off and retry.",
    500: "Internal server error: Anthropic backend fault; likely transient.",
    502: "Bad gateway: Anthropic proxy fault; transient.",
    503: "Service unavailable: Anthropic maintenance or overload; retry after backoff.",
    529: "Overloaded: Anthropic-specific API overloaded code; back off heavily.",
}


class LLMClient:
    """Wraps ChatAnthropic with L1/L2 caching and retry on Anthropic transient errors.

    Retried HTTP status codes from the Anthropic API:
      429 — Rate limited: too many requests per minute; back off and retry.
      500 — Internal server error: Anthropic backend fault; likely transient.
      502 — Bad gateway: Anthropic proxy fault; transient.
      503 — Service unavailable: Anthropic maintenance or overload; retry.
      529 — Overloaded: Anthropic-specific "API overloaded" code; back off heavily.

    Not retried:
      400 — Bad request (malformed prompt or parameters; permanent).
      401 — Auth failure (invalid API key; permanent).
      404 — Model not found (wrong model ID; permanent).
    """

    _RETRYABLE_STATUSES = {429, 500, 502, 503, 529}

    def __init__(self, inner: ChatAnthropic, redis_url: str) -> None:
        self._inner = inner
        self._redis_url = redis_url
        self._local = get_local_cache()

    @retry(**LLM_RETRY)
    def invoke(self, messages: list, **kwargs: Any):
        prompt_str = _messages_to_str(messages)
        key, compressed = make_cache_key(prompt_str)

        cached = self._local.get(key, compressed)
        if cached is not None:
            log.debug("LLM invoke L1 hit key=%s", key)
            return AIMessage(content=cached)

        cached = _llm_redis_get(self._redis_url, key, compressed)
        if cached is not None:
            log.debug("LLM invoke L2 hit key=%s — backfilling L1", key)
            self._local.set(key, compressed, cached)
            return AIMessage(content=cached)

        try:
            result = self._inner.invoke(messages, **kwargs)
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code in self._RETRYABLE_STATUSES:
                raise RetryableError(
                    status_code,
                    str(exc),
                    _LLM_RETRY_REASONS.get(status_code, "transient Anthropic API error"),
                )
            raise

        self._local.set(key, compressed, result.content)
        _llm_redis_set(self._redis_url, key, compressed, result.content)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


llm = LLMClient(
    ChatAnthropic(
        model=settings.llm_model,
        api_key=settings.anthropic_api_key,
        max_tokens=4096,
    ),
    redis_url=settings.redis_url,
)

_bounded_llm = LLMClient(
    ChatAnthropic(
        model=settings.llm_model,
        api_key=settings.anthropic_api_key,
        max_tokens=_AGENT_MAX_TOKENS,
    ),
    redis_url=settings.redis_url,
)


def get_bounded_llm_with_tools():
    """Returns 5000-token-capped LLM with code search tools. Used by the coder."""
    return _bounded_llm.bind_tools(get_code_search_tools())


def get_bounded_coder_llm_with_tools():
    """Returns 5000-token-capped LLM with the full ReAct coder tool set (search + file ops)."""
    from tools.langchain_tools import get_coder_tools
    return _bounded_llm.bind_tools(get_coder_tools())


def get_bounded_llm():
    """Returns 5000-token-capped plain LLM. Used by the tester."""
    return _bounded_llm
