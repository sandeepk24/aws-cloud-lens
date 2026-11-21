"""Application settings loaded from environment / .env."""

from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for cloudlens-api.

    AWS account IDs, regions, roles, and profiles are never set here —
    they come exclusively from the external accounts YAML referenced by
    ``CLOUDLENS_CONFIG_PATH``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="cloudlens-api", alias="APP_NAME")
    app_env: str = Field(default="development", alias="APP_ENV")
    app_debug: bool = Field(default=False, alias="APP_DEBUG")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    api_cors_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000",
        alias="API_CORS_ORIGINS",
    )

    database_url: str = Field(
        default="sqlite+pysqlite:///./data/cloudlens.db",
        alias="DATABASE_URL",
    )

    cloudlens_config_path: str | None = Field(
        default=None,
        alias="CLOUDLENS_CONFIG_PATH",
        description="Path to external accounts YAML (see config/accounts.example.yaml).",
    )

    cloudlens_export_dir: str = Field(
        default="./data/exports",
        alias="CLOUDLENS_EXPORT_DIR",
        description=(
            "Private directory for generated diagrams; artifacts are written to "
            "<dir>/<project>/<account_id>/ with owner-only permissions."
        ),
    )

    @field_validator("cloudlens_config_path", mode="before")
    @classmethod
    def _empty_config_path_as_none(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
