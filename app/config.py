from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = ""
    # These three values are required before the database can be reached and
    # intentionally remain deployment configuration. All product settings are
    # stored in the app_settings table and changed through /settings.
    owner_telegram_id: int | None = None
    database_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
