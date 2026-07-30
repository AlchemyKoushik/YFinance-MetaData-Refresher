from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "IES Metadata Refresh Service"
    log_level: str = "INFO"
    database_url: str = Field(..., min_length=1)
    catalog_path: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[1] / "data" / "ies_catalog.json")
    refresh_concurrency: int = Field(8, ge=1, le=32)
    refresh_timeout_seconds: float = Field(20.0, gt=0)
    refresh_max_attempts: int = Field(3, ge=1, le=5)
    retry_base_delay_seconds: float = Field(1.0, gt=0, le=5)
    db_pool_min_size: int = Field(1, ge=1, le=10)
    db_pool_max_size: int = Field(5, ge=1, le=20)
    db_command_timeout_seconds: float = Field(30.0, gt=0)
    startup_validation_ticker: str = "AAPL"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
