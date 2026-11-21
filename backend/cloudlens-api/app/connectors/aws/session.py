"""Thread-safe AWS session manager (Boto3 + STS).

Supports named profiles and AssumeRole. Uses only temporary credentials from
STS for role-based auth — never accepts or stores permanent access keys.
Validates caller identity via GetCallerIdentity and enforces that the live
account ID matches the configured expected account ID.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.accounts.loader import get_accounts_config
from app.accounts.models import AssumeRoleAuth, ProfileAuth
from app.connectors.aws.client import AWSConnector, get_aws_connector
from app.connectors.aws.errors import (
    AWSAccountMismatchError,
    AWSAuthenticationError,
    AWSReadOnlyViolationError,
    AWSRegionNotConfiguredError,
    AWSSessionRefreshError,
)
from app.connectors.aws.retry import call_with_backoff

logger = logging.getLogger(__name__)


class _Clock(Protocol):
    def __call__(self) -> datetime: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


# Operation-name prefixes inventory clients may call. Anything else (Create*,
# Delete*, Put*, Modify*, Run*, ...) is rejected before a request is signed.
READ_ONLY_OPERATION_PREFIXES: tuple[str, ...] = ("Describe", "List", "Get")


def _default_botocore_config() -> Config:
    """Standard botocore retries; region/service come from callers/config.

    ``max_attempts=3`` is the AWS SDK default for standard mode. Collectors and
    orchestration add their own jittered retries on top, so a larger value here
    multiplies worst-case requests per page (previously 10 x 5 x 5 = 250).
    """
    return Config(
        retries={"max_attempts": 3, "mode": "standard"},
        connect_timeout=5,
        read_timeout=60,
        max_pool_connections=10,
    )


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    """Result of STS GetCallerIdentity (no secrets)."""

    account_id: str
    arn: str
    user_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "account_id": self.account_id,
            "arn": self.arn,
            "user_id": self.user_id,
        }


@dataclass(frozen=True, slots=True)
class _TemporaryCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: datetime


class AWSSessionManager:
    """Manage Boto3 sessions for one configured account connector.

    Thread-safe: credential refresh and session replacement are guarded by a lock.

    Permanent access keys are never accepted as constructor arguments. AssumeRole
    sessions are built exclusively from STS temporary credentials (access key +
    secret + session token + expiration).
    """

    def __init__(
        self,
        connector: AWSConnector,
        *,
        sts_service_name: str,
        botocore_config: Config | None = None,
        session_factory: Callable[..., Any] | None = None,
        refresh_skew: timedelta = timedelta(minutes=5),
        clock: _Clock = _utc_now,
        max_retry_attempts: int = 5,
        validate_on_init: bool = False,
        allowed_operation_prefixes: tuple[str, ...] = READ_ONLY_OPERATION_PREFIXES,
    ) -> None:
        if not sts_service_name.strip():
            raise ValueError("sts_service_name must be a non-empty AWS service name")

        self._connector = connector
        self._sts_service_name = sts_service_name.strip()
        self._botocore_config = botocore_config or _default_botocore_config()
        self._session_factory = session_factory or boto3.Session
        self._refresh_skew = refresh_skew
        self._clock = clock
        self._max_retry_attempts = max_retry_attempts

        self._lock = threading.RLock()
        self._session: Any | None = None
        self._temp_credentials: _TemporaryCredentials | None = None
        self._identity: CallerIdentity | None = None
        self._allowed_prefixes = tuple(allowed_operation_prefixes)
        # Clients are thread-safe and expensive to build (service model load,
        # endpoint resolution, new connection pool). Cache per (service, region)
        # for the lifetime of the current credentials.
        self._clients: dict[tuple[str, str], BaseClient] = {}
        self._api_calls: Counter[str] = Counter()
        self._api_calls_lock = threading.Lock()

        if validate_on_init:
            self.validate_identity()

    @property
    def connector(self) -> AWSConnector:
        return self._connector

    @property
    def identity(self) -> CallerIdentity | None:
        with self._lock:
            return self._identity

    def get_session(self, *, force_refresh: bool = False) -> Any:
        """Return a usable Boto3 session, refreshing temporary credentials if needed."""
        with self._lock:
            self._ensure_session(force_refresh=force_refresh)
            assert self._session is not None
            return self._session

    def client(
        self,
        service_name: str,
        region: str | None = None,
        *,
        require_configured_region: bool = True,
    ) -> BaseClient:
        """Create a region-scoped Boto3 client.

        ``service_name`` and ``region`` must be supplied by the caller/config —
        they are not hardcoded here. ``region`` defaults to the account primary
        region. When ``require_configured_region`` is true (default), the region
        must appear in the account's configured region list. Global control-plane
        callers (Route 53, CloudFront-scope WAF) may set it to false and pass a
        configured ``global_control_region`` instead.
        """
        if not service_name or not service_name.strip():
            raise ValueError("service_name must be a non-empty string")
        region_name = region or self._connector.primary_region
        if require_configured_region:
            self._assert_region(region_name)
        key = (service_name.strip(), region_name)
        # boto3.Session is not thread-safe for client creation; build under the
        # same lock that guards credential refresh.
        with self._lock:
            self._ensure_session(force_refresh=False)
            cached = self._clients.get(key)
            if cached is not None:
                return cached
            assert self._session is not None
            try:
                client = self._session.client(
                    key[0],
                    region_name=region_name,
                    config=self._botocore_config,
                )
            except Exception as exc:
                raise AWSAuthenticationError(
                    f"Failed to create AWS client '{service_name}' in region "
                    f"'{region_name}' for account '{self._connector.account_alias}': {exc}",
                    cause=exc,
                ) from exc
            self._install_guards(client, key[0])
            self._clients[key] = client
            return client

    def api_call_counts(self) -> dict[str, int]:
        """AWS operations invoked through this manager's clients (incl. retries)."""
        with self._api_calls_lock:
            return dict(self._api_calls)

    def _install_guards(self, client: Any, service_name: str) -> None:
        """Reject non-read-only operations and count calls per operation."""
        events = getattr(getattr(client, "meta", None), "events", None)
        if events is None or not hasattr(events, "register"):
            return
        allowed = self._allowed_prefixes
        account_alias = self._connector.account_alias

        def _before_call(model: Any = None, **_kwargs: Any) -> None:
            operation = str(getattr(model, "name", "") or "")
            if not operation.startswith(allowed):
                raise AWSReadOnlyViolationError(
                    f"Blocked non-read-only AWS operation {service_name}:{operation} "
                    f"for account '{account_alias}'"
                )
            with self._api_calls_lock:
                self._api_calls[f"{service_name}:{operation}"] += 1

        # botocore dispatches more specific event names first and stops at the
        # first handler that returns a response; registering first on the most
        # specific pattern guarantees the guard runs before any other hook.
        register_first = getattr(events, "register_first", None)
        if callable(register_first):
            register_first("before-call.*.*", _before_call)
        else:
            events.register("before-call.*.*", _before_call)

    def validate_identity(self, *, force_refresh: bool = False) -> CallerIdentity:
        """Call GetCallerIdentity and enforce expected account ID."""
        with self._lock:
            self._ensure_session(force_refresh=force_refresh)
            identity = self._fetch_caller_identity()
            self._assert_expected_account(identity)
            self._identity = identity
            return identity

    def refresh(self) -> CallerIdentity:
        """Force credential refresh and re-validate identity."""
        return self.validate_identity(force_refresh=True)

    def describe(self) -> dict[str, Any]:
        """Non-secret diagnostics for the active session."""
        with self._lock:
            expiry = (
                self._temp_credentials.expiration.isoformat()
                if self._temp_credentials
                else None
            )
            return {
                **self._connector.describe(),
                "session_ready": self._session is not None,
                "auth_type": self._connector.auth.type,
                "temporary_credentials": self._temp_credentials is not None,
                "credentials_expiration": expiry,
                "identity": self._identity.to_dict() if self._identity else None,
                "live_calls_enabled": True,
                "cached_clients": len(self._clients),
                "api_calls": self.api_call_counts(),
            }

    def _assert_region(self, region: str) -> None:
        if region not in self._connector.regions:
            raise AWSRegionNotConfiguredError(
                f"Region '{region}' is not configured for account "
                f"'{self._connector.account_alias}' in project "
                f"'{self._connector.project_alias}'"
            )

    def _ensure_session(self, *, force_refresh: bool) -> None:
        if force_refresh or self._session is None or self._credentials_need_refresh():
            self._refresh_session()

    def _credentials_need_refresh(self) -> bool:
        if self._temp_credentials is None:
            return False
        now = self._clock()
        expires_at = self._temp_credentials.expiration
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return now >= (expires_at - self._refresh_skew)

    def _refresh_session(self) -> None:
        auth = self._connector.auth
        # Clients hold the old credentials; drop them with the session.
        self._clients.clear()
        try:
            if isinstance(auth, ProfileAuth):
                self._refresh_with_profile(auth)
            elif isinstance(auth, AssumeRoleAuth):
                self._refresh_with_assume_role(auth)
            else:
                raise AWSAuthenticationError(
                    f"Unsupported auth type for account "
                    f"'{self._connector.account_alias}': {type(auth)!r}"
                )
        except AWSAuthenticationError:
            raise
        except (ClientError, BotoCoreError) as exc:
            raise AWSSessionRefreshError(
                self._format_auth_failure("session refresh", exc),
                cause=exc,
            ) from exc
        except Exception as exc:
            raise AWSSessionRefreshError(
                self._format_auth_failure("session refresh", exc),
                cause=exc,
            ) from exc

    def _refresh_with_profile(self, auth: ProfileAuth) -> None:
        session = self._session_factory(
            profile_name=auth.profile,
            region_name=self._connector.primary_region,
        )
        self._session = session
        self._temp_credentials = None
        logger.info(
            "AWS session ready via profile for project=%s account=%s",
            self._connector.project_alias,
            self._connector.account_alias,
        )

    def _refresh_with_assume_role(self, auth: AssumeRoleAuth) -> None:
        base_kwargs: dict[str, Any] = {
            "region_name": self._connector.sts_region(),
        }
        if auth.source_profile:
            base_kwargs["profile_name"] = auth.source_profile

        base_session = self._session_factory(**base_kwargs)
        sts = base_session.client(
            self._sts_service_name,
            region_name=self._connector.sts_region(),
            config=self._botocore_config,
        )

        assume_kwargs = self._connector.build_assume_role_kwargs()
        if "RoleSessionName" not in assume_kwargs:
            assume_kwargs["RoleSessionName"] = (
                f"cloudlens-{self._connector.account_alias}"[:64]
            )

        response = call_with_backoff(
            lambda: sts.assume_role(**assume_kwargs),
            max_attempts=self._max_retry_attempts,
        )
        temp = self._parse_temporary_credentials(response)
        # Managed session uses only STS temporary credentials (token required).
        self._session = self._session_factory(
            aws_access_key_id=temp.access_key_id,
            aws_secret_access_key=temp.secret_access_key,
            aws_session_token=temp.session_token,
            region_name=self._connector.primary_region,
        )
        self._temp_credentials = temp
        logger.info(
            "AWS temporary credentials assumed for project=%s account=%s expires=%s",
            self._connector.project_alias,
            self._connector.account_alias,
            temp.expiration.isoformat(),
        )

    def _parse_temporary_credentials(
        self, response: Mapping[str, Any]
    ) -> _TemporaryCredentials:
        creds = response.get("Credentials")
        if not isinstance(creds, Mapping):
            raise AWSSessionRefreshError(
                "STS AssumeRole response missing Credentials for "
                f"account '{self._connector.account_alias}'"
            )

        access_key = creds.get("AccessKeyId")
        secret_key = creds.get("SecretAccessKey")
        token = creds.get("SessionToken")
        expiration = creds.get("Expiration")
        if not access_key or not secret_key or not token or expiration is None:
            raise AWSSessionRefreshError(
                "STS AssumeRole returned incomplete temporary credentials for "
                f"account '{self._connector.account_alias}' "
                "(session token is required; permanent keys are not supported)"
            )
        if isinstance(expiration, datetime):
            exp = expiration if expiration.tzinfo else expiration.replace(tzinfo=UTC)
        else:
            raise AWSSessionRefreshError(
                "STS AssumeRole Expiration must be a datetime for "
                f"account '{self._connector.account_alias}'"
            )
        return _TemporaryCredentials(
            access_key_id=str(access_key),
            secret_access_key=str(secret_key),
            session_token=str(token),
            expiration=exp,
        )

    def _fetch_caller_identity(self) -> CallerIdentity:
        assert self._session is not None
        if isinstance(self._connector.auth, AssumeRoleAuth):
            region_name = self._connector.sts_region()
        else:
            region_name = self._connector.primary_region

        sts = self._session.client(
            self._sts_service_name,
            region_name=region_name,
            config=self._botocore_config,
        )
        try:
            response = call_with_backoff(
                lambda: sts.get_caller_identity(),
                max_attempts=self._max_retry_attempts,
            )
        except (ClientError, BotoCoreError) as exc:
            raise AWSAuthenticationError(
                self._format_auth_failure("GetCallerIdentity", exc),
                cause=exc,
            ) from exc

        account = response.get("Account")
        arn = response.get("Arn")
        user_id = response.get("UserId")
        if not account or not arn or not user_id:
            raise AWSAuthenticationError(
                "GetCallerIdentity returned an incomplete identity payload for "
                f"account '{self._connector.account_alias}'"
            )
        return CallerIdentity(
            account_id=str(account),
            arn=str(arn),
            user_id=str(user_id),
        )

    def _assert_expected_account(self, identity: CallerIdentity) -> None:
        expected = self._connector.account_id
        if identity.account_id != expected:
            raise AWSAccountMismatchError(
                expected_account_id=expected,
                actual_account_id=identity.account_id,
                project_alias=self._connector.project_alias,
                account_alias=self._connector.account_alias,
            )

    def _format_auth_failure(self, operation: str, exc: BaseException) -> str:
        detail = str(exc)
        if isinstance(exc, ClientError):
            error = exc.response.get("Error", {})
            code = error.get("Code", "Unknown")
            message = error.get("Message", detail)
            detail = f"{code}: {message}"
        return (
            f"AWS authentication failed during {operation} for project "
            f"'{self._connector.project_alias}' account "
            f"'{self._connector.account_alias}' ({self._connector.auth.type}): {detail}"
        )


def create_session_manager(
    connector: AWSConnector,
    *,
    sts_service_name: str,
    validate: bool = False,
    **kwargs: Any,
) -> AWSSessionManager:
    """Factory for an ``AWSSessionManager`` bound to a connector."""
    return AWSSessionManager(
        connector,
        sts_service_name=sts_service_name,
        validate_on_init=validate,
        **kwargs,
    )


def create_session_manager_for_account(
    project_alias: str,
    account_alias: str,
    *,
    validate: bool = False,
    **kwargs: Any,
) -> AWSSessionManager:
    """Build a session manager using accounts YAML (incl. STS service name)."""
    config = get_accounts_config()
    connector = get_aws_connector(project_alias, account_alias)
    return create_session_manager(
        connector,
        sts_service_name=config.aws_api.sts_service_name,
        validate=validate,
        **kwargs,
    )
