"""Load and validate accounts YAML from CLOUDLENS_CONFIG_PATH."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.accounts.models import AccountsConfig
from app.core.config import get_settings


class AccountsConfigError(Exception):
    """Raised when the accounts configuration cannot be loaded or validated."""


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Resolve the accounts YAML path.

    Precedence:
    1. ``explicit`` argument
    2. ``CLOUDLENS_CONFIG_PATH`` / settings
    """
    if explicit is not None:
        return Path(explicit).expanduser().resolve()

    settings = get_settings()
    if not settings.cloudlens_config_path:
        raise AccountsConfigError(
            "Accounts configuration path is not set. "
            "Set CLOUDLENS_CONFIG_PATH to a YAML file "
            "(see config/accounts.example.yaml)."
        )
    return Path(settings.cloudlens_config_path).expanduser().resolve()


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise AccountsConfigError(f"Accounts configuration file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AccountsConfigError(
            f"Unable to read accounts configuration file: {path}"
        ) from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise AccountsConfigError(
            f"Invalid YAML in accounts configuration file: {path}"
        ) from exc
    if data is None:
        raise AccountsConfigError(f"Accounts configuration file is empty: {path}")
    if not isinstance(data, dict):
        raise AccountsConfigError(
            f"Accounts configuration root must be a mapping: {path}"
        )
    return data


def load_accounts_config(path: str | Path | None = None) -> AccountsConfig:
    """Load, parse, and validate the accounts configuration document."""
    resolved = resolve_config_path(path)
    payload = _read_yaml(resolved)
    try:
        return AccountsConfig.model_validate(payload)
    except ValidationError as exc:
        raise AccountsConfigError(
            f"Accounts configuration validation failed for {resolved}:\n{exc}"
        ) from exc


@lru_cache
def get_accounts_config() -> AccountsConfig:
    """Cached loader for process lifetime (clear cache in tests)."""
    return load_accounts_config()


def clear_accounts_config_cache() -> None:
    get_accounts_config.cache_clear()
