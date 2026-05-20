import logging
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from tools.langchain_tools import get_code_search_tools

# Explicitly load .env into os.environ before Settings is instantiated.
# pydantic-settings also reads env_file directly, but calling load_dotenv()
# here ensures os.getenv() calls anywhere else in the process see the same values.
load_dotenv()


class Settings(BaseSettings):
    # ── Anthropic ──────────────────────────────────────────────────────────────
    anthropic_api_key: str

    # ── GitHub — PAT or App (at least one must be set) ─────────────────────────
    github_token: str = ""
    github_app_id: str = ""
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

    # ── GitHub OAuth ───────────────────────────────────────────────────────────
    github_oauth_client_id: str
    github_oauth_client_secret: SecretStr

    # ── Google OAuth ───────────────────────────────────────────────────────────
    google_oauth_client_id: str
    google_oauth_client_secret: SecretStr

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

llm = ChatAnthropic(
    model=settings.llm_model,
    api_key=settings.anthropic_api_key,
    max_tokens=4096,
)

_bounded_llm = ChatAnthropic(
    model=settings.llm_model,
    api_key=settings.anthropic_api_key,
    max_tokens=_AGENT_MAX_TOKENS,
)


def get_llm_with_tools():
    """Returns LLM client with code search tools bound.

    Used by llm_nodes for code generation with access to project structure.
    """
    tools = get_code_search_tools()
    return llm.bind_tools(tools)


def get_bounded_llm_with_tools():
    """Returns 5000-token-capped LLM with code search tools. Used by the coder."""
    return _bounded_llm.bind_tools(get_code_search_tools())


def get_bounded_llm():
    """Returns 5000-token-capped plain LLM. Used by the tester."""
    return _bounded_llm
