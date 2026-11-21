"""AWS connector — session / assume-role kwargs from external accounts config.

No live AWS API calls. Regions, profiles, role ARNs, and account IDs are
taken only from validated account configuration — never hardcoded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.accounts.loader import get_accounts_config
from app.accounts.models import AccountConfig, AssumeRoleAuth, ProfileAuth, ProjectConfig


@dataclass(frozen=True, slots=True)
class AWSConnector:
    """Connector bound to one configured project/account pair."""

    project_alias: str
    account_alias: str
    account_id: str
    regions: tuple[str, ...]
    enabled_collectors: tuple[str, ...]
    auth: ProfileAuth | AssumeRoleAuth

    @property
    def primary_region(self) -> str:
        """First configured region for this account (config-driven)."""
        return self.regions[0]

    def describe(self) -> dict[str, Any]:
        """Return non-secret connection metadata for diagnostics."""
        meta: dict[str, Any] = {
            "provider": "aws",
            "project_alias": self.project_alias,
            "account_alias": self.account_alias,
            "account_id": self.account_id,
            "regions": list(self.regions),
            "enabled_collectors": list(self.enabled_collectors),
            "auth_type": self.auth.type,
            "live_calls_enabled": False,
        }
        if isinstance(self.auth, ProfileAuth):
            meta["profile"] = self.auth.profile
        else:
            meta["role_arn"] = self.auth.role_arn
            if self.auth.source_profile:
                meta["source_profile"] = self.auth.source_profile
            if self.auth.sts_region:
                meta["sts_region"] = self.auth.sts_region
        return meta

    def build_session_kwargs(self, region: str | None = None) -> dict[str, Any]:
        """Kwargs suitable for a future ``boto3.Session`` construction.

        ``region`` must be one of the account's configured regions when provided.
        """
        region_name = region or self.primary_region
        self._assert_region(region_name)
        kwargs: dict[str, Any] = {"region_name": region_name}
        if isinstance(self.auth, ProfileAuth):
            kwargs["profile_name"] = self.auth.profile
        elif self.auth.source_profile:
            kwargs["profile_name"] = self.auth.source_profile
        return kwargs

    def build_assume_role_kwargs(self) -> dict[str, Any]:
        """Kwargs for a future STS AssumeRole call (no API invocation here)."""
        if not isinstance(self.auth, AssumeRoleAuth):
            raise ValueError(
                f"Account '{self.account_alias}' auth type is "
                f"'{self.auth.type}', not assume_role"
            )
        kwargs: dict[str, Any] = {"RoleArn": self.auth.role_arn}
        if self.auth.session_name:
            kwargs["RoleSessionName"] = self.auth.session_name
        if self.auth.external_id:
            kwargs["ExternalId"] = self.auth.external_id
        if self.auth.duration_seconds is not None:
            kwargs["DurationSeconds"] = self.auth.duration_seconds
        return kwargs

    def sts_region(self) -> str:
        """Region used for STS, from auth override or the account's primary region."""
        if isinstance(self.auth, AssumeRoleAuth) and self.auth.sts_region:
            return self.auth.sts_region
        return self.primary_region

    def _assert_region(self, region: str) -> None:
        if region not in self.regions:
            raise ValueError(
                f"Region '{region}' is not configured for account "
                f"'{self.account_alias}' in project '{self.project_alias}'"
            )


def connector_from_account(
    project: ProjectConfig, account: AccountConfig
) -> AWSConnector:
    return AWSConnector(
        project_alias=project.alias,
        account_alias=account.alias,
        account_id=account.account_id,
        regions=tuple(account.regions),
        enabled_collectors=tuple(account.collectors.enabled),
        auth=account.auth,
    )


def create_aws_connectors() -> list[AWSConnector]:
    """Build connectors for every account in the external accounts YAML."""
    config = get_accounts_config()
    return [
        connector_from_account(project, account)
        for project, account in config.iter_accounts()
    ]


def get_aws_connector(project_alias: str, account_alias: str) -> AWSConnector:
    """Return the connector for a specific project/account alias pair."""
    config = get_accounts_config()
    project, account = config.find_account(project_alias, account_alias)
    return connector_from_account(project, account)


@lru_cache
def get_default_aws_connector() -> AWSConnector:
    """First configured account connector (requires CLOUDLENS_CONFIG_PATH)."""
    connectors = create_aws_connectors()
    if not connectors:
        raise RuntimeError("Accounts configuration contains no accounts")
    return connectors[0]
