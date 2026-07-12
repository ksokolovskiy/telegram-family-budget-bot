from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = ""
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    database_url: str = "postgresql+asyncpg://budget:budget@postgres:5432/budget_bot"
    log_level: str = "INFO"
    app_env: str = "local"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
