"""Clear authentication and session errors for AWS connectors."""

from __future__ import annotations


class AWSAuthenticationError(Exception):
    """Base error for AWS authentication / session failures."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class AWSAccountMismatchError(AWSAuthenticationError):
    """Caller identity account does not match the configured expected account ID."""

    def __init__(
        self,
        *,
        expected_account_id: str,
        actual_account_id: str,
        project_alias: str,
        account_alias: str,
    ) -> None:
        self.expected_account_id = expected_account_id
        self.actual_account_id = actual_account_id
        self.project_alias = project_alias
        self.account_alias = account_alias
        super().__init__(
            "AWS caller identity account mismatch for "
            f"project '{project_alias}' account '{account_alias}': "
            f"expected {expected_account_id}, got {actual_account_id}"
        )


class AWSSessionRefreshError(AWSAuthenticationError):
    """Temporary credentials could not be obtained or refreshed."""


class AWSRegionNotConfiguredError(AWSAuthenticationError):
    """Requested region is not enabled for the configured account."""


class AWSReadOnlyViolationError(AWSAuthenticationError):
    """A collector client attempted an operation outside the read-only allowlist."""
