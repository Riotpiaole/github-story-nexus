import logging
import os
from functools import lru_cache
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Anthropic
    anthropic_api_key: str

    # GitHub — PAT or App (at least one must be set)
    github_token: str = ""
    github_app_id: str = ""
    github_private_key_path: str = ""
    github_installation_id: int = 0
    github_webhook_secret: str = ""

    # Agent tuning
    max_retries: int = 3
    llm_model: str = "claude-sonnet-4-6"
    base_branch: str = "main"

    # LangSmith
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "story-pr-agent"

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

llm = ChatAnthropic(
    model=settings.llm_model,
    api_key=settings.anthropic_api_key,
    max_tokens=4096,
)
