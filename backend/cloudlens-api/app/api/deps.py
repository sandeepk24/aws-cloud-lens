"""Shared FastAPI dependencies."""

from app.accounts import AccountsConfig, get_accounts_config
from app.analytics.engine import AnalyticsEngine
from app.collectors.base import CollectorRegistry
from app.connectors.aws.client import AWSConnector, create_aws_connectors
from app.connectors.aws.session import AWSSessionManager, create_session_manager
from app.core.config import Settings, get_settings


def settings_dep() -> Settings:
    return get_settings()


def accounts_config_dep() -> AccountsConfig:
    return get_accounts_config()


def aws_connectors_dep() -> list[AWSConnector]:
    return create_aws_connectors()


def aws_session_manager_dep(
    project_alias: str, account_alias: str
) -> AWSSessionManager:
    """Build a session manager for a configured project/account pair."""
    config = get_accounts_config()
    connectors = {
        (c.project_alias, c.account_alias): c for c in create_aws_connectors()
    }
    try:
        connector = connectors[(project_alias, account_alias)]
    except KeyError as exc:
        raise KeyError(
            f"account '{account_alias}' not found in project '{project_alias}'"
        ) from exc
    return create_session_manager(
        connector,
        sts_service_name=config.aws_api.sts_service_name,
    )


def collector_registry_dep() -> CollectorRegistry:
    """Return an empty registry until concrete collectors are registered."""
    return CollectorRegistry()


def analytics_engine_dep() -> AnalyticsEngine:
    return AnalyticsEngine()
