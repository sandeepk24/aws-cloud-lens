"""Tests for AWSSessionManager using mocked AWS responses."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.accounts.loader import load_accounts_config
from app.connectors.aws.client import connector_from_account
from app.connectors.aws.errors import (
    AWSAccountMismatchError,
    AWSAuthenticationError,
    AWSRegionNotConfiguredError,
    AWSSessionRefreshError,
)
from app.connectors.aws.session import AWSSessionManager, create_session_manager

VALID_YAML = Path(__file__).parent / "fixtures" / "accounts.valid.yaml"


def _client_error(code: str, message: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "AssumeRole",
    )


def _profile_connector():
    config = load_accounts_config(VALID_YAML)
    project, account = config.find_account("demo-project", "profile-account")
    return connector_from_account(project, account), config.aws_api.sts_service_name


def _role_connector():
    config = load_accounts_config(VALID_YAML)
    project, account = config.find_account("demo-project", "role-account")
    return connector_from_account(project, account), config.aws_api.sts_service_name


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def test_profile_session_validates_matching_account() -> None:
    connector, sts_name = _profile_connector()
    session = MagicMock(name="profile-session")
    sts = MagicMock(name="sts")
    sts.get_caller_identity.return_value = {
        "Account": "111122223333",
        "Arn": "arn:aws:iam::111122223333:user/test",
        "UserId": "AIDATEST",
    }
    session.client.return_value = sts
    factory = MagicMock(return_value=session)

    manager = AWSSessionManager(
        connector,
        sts_service_name=sts_name,
        session_factory=factory,
    )
    identity = manager.validate_identity()

    assert identity.account_id == "111122223333"
    factory.assert_called_once_with(
        profile_name="test-profile",
        region_name="test-region-a",
    )
    session.client.assert_called_with(
        sts_name,
        region_name="test-region-a",
        config=manager._botocore_config,
    )


def test_profile_session_rejects_account_mismatch() -> None:
    connector, sts_name = _profile_connector()
    session = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {
        "Account": "999999999999",
        "Arn": "arn:aws:iam::999999999999:user/other",
        "UserId": "AIDAOTHER",
    }
    session.client.return_value = sts

    manager = AWSSessionManager(
        connector,
        sts_service_name=sts_name,
        session_factory=MagicMock(return_value=session),
    )
    with pytest.raises(AWSAccountMismatchError) as exc_info:
        manager.validate_identity()
    assert exc_info.value.expected_account_id == "111122223333"
    assert exc_info.value.actual_account_id == "999999999999"


def test_assume_role_uses_temporary_credentials_only() -> None:
    connector, sts_name = _role_connector()
    expiration = datetime(2030, 1, 1, tzinfo=UTC)

    base_session = MagicMock(name="base-session")
    sts = MagicMock(name="sts")
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "ASIATEMP",
            "SecretAccessKey": "secret-temp",
            "SessionToken": "token-temp",
            "Expiration": expiration,
        }
    }
    base_session.client.return_value = sts

    assumed_session = MagicMock(name="assumed-session")
    assumed_sts = MagicMock(name="assumed-sts")
    assumed_sts.get_caller_identity.return_value = {
        "Account": "444455556666",
        "Arn": "arn:aws:sts::444455556666:assumed-role/TestReadOnly/cloudlens-test",
        "UserId": "AROATEST:cloudlens-test",
    }
    assumed_session.client.return_value = assumed_sts

    sessions: list[Any] = [base_session, assumed_session]
    factory = MagicMock(side_effect=sessions)

    manager = create_session_manager(
        connector,
        sts_service_name=sts_name,
        session_factory=factory,
    )
    identity = manager.validate_identity()
    assert identity.account_id == "444455556666"

    # First call: source profile session; second: temporary credentials only.
    assert factory.call_count == 2
    assumed_kwargs = factory.call_args_list[1].kwargs
    assert assumed_kwargs["aws_access_key_id"] == "ASIATEMP"
    assert assumed_kwargs["aws_secret_access_key"] == "secret-temp"
    assert assumed_kwargs["aws_session_token"] == "token-temp"
    assert "profile_name" not in assumed_kwargs

    sts.assume_role.assert_called_once()
    assume_kwargs = sts.assume_role.call_args.kwargs
    assert assume_kwargs["RoleArn"] == "arn:aws:iam::444455556666:role/TestReadOnly"
    assert assume_kwargs["RoleSessionName"] == "cloudlens-test"
    assert assume_kwargs["ExternalId"] == "test-external-id"
    assert assume_kwargs["DurationSeconds"] == 3600

    desc = manager.describe()
    assert desc["temporary_credentials"] is True
    assert desc["credentials_expiration"] == expiration.isoformat()


def test_assume_role_refreshes_near_expiry() -> None:
    connector, sts_name = _role_connector()
    clock = _FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    first_exp = clock.now + timedelta(minutes=10)
    second_exp = clock.now + timedelta(hours=1)

    def _assume_response(expiration: datetime) -> dict[str, Any]:
        return {
            "Credentials": {
                "AccessKeyId": f"ASIA-{expiration.minute}",
                "SecretAccessKey": "secret",
                "SessionToken": f"token-{expiration.isoformat()}",
                "Expiration": expiration,
            }
        }

    base_session = MagicMock()
    sts = MagicMock()
    sts.assume_role.side_effect = [
        _assume_response(first_exp),
        _assume_response(second_exp),
    ]
    base_session.client.return_value = sts

    assumed_sessions = [MagicMock(name="s1"), MagicMock(name="s2")]
    for assumed in assumed_sessions:
        assumed_sts = MagicMock()
        assumed_sts.get_caller_identity.return_value = {
            "Account": "444455556666",
            "Arn": "arn:aws:sts::444455556666:assumed-role/TestReadOnly/x",
            "UserId": "AROATEST:x",
        }
        assumed.client.return_value = assumed_sts

    factory = MagicMock(
        side_effect=[base_session, assumed_sessions[0], base_session, assumed_sessions[1]]
    )

    manager = AWSSessionManager(
        connector,
        sts_service_name=sts_name,
        session_factory=factory,
        clock=clock,
        refresh_skew=timedelta(minutes=5),
    )
    manager.validate_identity()
    assert sts.assume_role.call_count == 1

    # Still outside skew window — no refresh.
    clock.advance(timedelta(minutes=2))
    manager.get_session()
    assert sts.assume_role.call_count == 1

    # Enter skew window — refresh temporary credentials.
    clock.advance(timedelta(minutes=4))
    manager.get_session()
    assert sts.assume_role.call_count == 2


def test_region_scoped_client_rejects_unconfigured_region() -> None:
    connector, sts_name = _profile_connector()
    session = MagicMock()
    manager = AWSSessionManager(
        connector,
        sts_service_name=sts_name,
        session_factory=MagicMock(return_value=session),
    )
    with pytest.raises(AWSRegionNotConfiguredError):
        manager.client("ec2", region="not-configured")


def test_region_scoped_client_uses_configured_region() -> None:
    connector, sts_name = _role_connector()
    expiration = datetime(2030, 1, 1, tzinfo=UTC)
    base_session = MagicMock()
    sts = MagicMock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "ASIA",
            "SecretAccessKey": "secret",
            "SessionToken": "token",
            "Expiration": expiration,
        }
    }
    base_session.client.return_value = sts
    assumed = MagicMock()
    service_client = MagicMock(name="service")
    assumed.client.return_value = service_client
    factory = MagicMock(side_effect=[base_session, assumed])

    manager = AWSSessionManager(
        connector,
        sts_service_name=sts_name,
        session_factory=factory,
    )
    client = manager.client("ec2", region="test-region-c")
    assert client is service_client
    assumed.client.assert_called_with(
        "ec2",
        region_name="test-region-c",
        config=manager._botocore_config,
    )


def test_clear_error_on_assume_role_failure() -> None:
    connector, sts_name = _role_connector()
    base_session = MagicMock()
    sts = MagicMock()
    sts.assume_role.side_effect = _client_error(
        "AccessDenied", "Not authorized to perform sts:AssumeRole"
    )
    base_session.client.return_value = sts

    manager = AWSSessionManager(
        connector,
        sts_service_name=sts_name,
        session_factory=MagicMock(return_value=base_session),
        max_retry_attempts=1,
    )
    with pytest.raises(AWSSessionRefreshError, match="AccessDenied"):
        manager.get_session()


def test_clear_error_on_get_caller_identity_failure() -> None:
    connector, sts_name = _profile_connector()
    session = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.side_effect = _client_error(
        "ExpiredToken", "Token expired", status=403
    )
    session.client.return_value = sts

    manager = AWSSessionManager(
        connector,
        sts_service_name=sts_name,
        session_factory=MagicMock(return_value=session),
        max_retry_attempts=1,
    )
    with pytest.raises(AWSAuthenticationError, match="GetCallerIdentity"):
        manager.validate_identity()


def test_assume_role_retries_throttling_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector, sts_name = _role_connector()
    expiration = datetime(2030, 1, 1, tzinfo=UTC)
    base_session = MagicMock()
    sts = MagicMock()
    sts.assume_role.side_effect = [
        _client_error("Throttling", "Rate exceeded", status=400),
        {
            "Credentials": {
                "AccessKeyId": "ASIA",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": expiration,
            }
        },
    ]
    base_session.client.return_value = sts
    assumed = MagicMock()
    factory = MagicMock(side_effect=[base_session, assumed])
    sleeps: list[float] = []

    import app.connectors.aws.retry as retry_mod
    import app.connectors.aws.session as session_mod

    original = retry_mod.call_with_backoff

    def _fast_backoff(operation, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["sleep"] = lambda seconds: sleeps.append(seconds)
        kwargs.setdefault("rng", lambda: 0.0)
        return original(operation, **kwargs)

    monkeypatch.setattr(session_mod, "call_with_backoff", _fast_backoff)

    manager = AWSSessionManager(
        connector,
        sts_service_name=sts_name,
        session_factory=factory,
        max_retry_attempts=3,
    )
    manager.get_session()

    assert sts.assume_role.call_count == 2
    assert len(sleeps) == 1


def test_thread_safe_concurrent_refresh() -> None:
    connector, sts_name = _role_connector()
    expiration = datetime(2030, 1, 1, tzinfo=UTC)
    call_count = {"n": 0}
    lock = threading.Lock()

    def assume_role(**_kwargs: Any) -> dict[str, Any]:
        with lock:
            call_count["n"] += 1
        return {
            "Credentials": {
                "AccessKeyId": "ASIA",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": expiration,
            }
        }

    base_session = MagicMock()
    sts = MagicMock()
    sts.assume_role.side_effect = assume_role
    base_session.client.return_value = sts

    def session_factory(**kwargs: Any) -> MagicMock:
        if "aws_session_token" in kwargs:
            return MagicMock(name="assumed")
        return base_session

    manager = AWSSessionManager(
        connector,
        sts_service_name=sts_name,
        session_factory=session_factory,
    )

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            manager.get_session()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    # First refresh wins; others reuse the session under the lock.
    assert call_count["n"] == 1


def test_incomplete_temporary_credentials_rejected() -> None:
    connector, sts_name = _role_connector()
    base_session = MagicMock()
    sts = MagicMock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "AKIAPermanentLooking",
            "SecretAccessKey": "secret",
            # Missing SessionToken — permanent keys not allowed
            "Expiration": datetime(2030, 1, 1, tzinfo=UTC),
        }
    }
    base_session.client.return_value = sts

    manager = AWSSessionManager(
        connector,
        sts_service_name=sts_name,
        session_factory=MagicMock(return_value=base_session),
    )
    with pytest.raises(AWSSessionRefreshError, match="session token is required"):
        manager.get_session()
