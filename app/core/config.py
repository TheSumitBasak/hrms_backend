"""Application configuration loaded from environment variables / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/app/core/config.py -> backend/
BACKEND_DIR = Path(__file__).resolve().parents[2]
# Resolve .env relative to the project (not the current working directory) so the
# app and seed script behave the same no matter where they are launched from.
ENV_FILE = BACKEND_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Keka-style HRMS"
    environment: str = "local"
    debug: bool = True
    api_prefix: str = "/api"

    database_url: str = "mysql+pymysql://root:admin@127.0.0.1:3306/hrms?charset=utf8mb4"

    secret_key: str = "change-me-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 720
    refresh_token_expire_days: int = 30

    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    seed_admin_email: str = "admin@example.com"
    seed_admin_password: str = "Admin@12345"

    # When true, seed baseline + demo data automatically at startup if the
    # database is empty. Handy for local/dev; leave false in production.
    auto_seed: bool = False

    # Pagination
    default_page_size: int = 25
    max_page_size: int = 100

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_mysql(self) -> bool:
        return self.database_url.startswith(("mysql", "mariadb"))


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
