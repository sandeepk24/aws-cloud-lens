"""AWS connector package."""

from app.connectors.aws.client import (
    AWSConnector,
    connector_from_account,
    create_aws_connectors,
    get_aws_connector,
    get_default_aws_connector,
)
from app.connectors.aws.errors import (
    AWSAccountMismatchError,
    AWSAuthenticationError,
    AWSReadOnlyViolationError,
    AWSRegionNotConfiguredError,
    AWSSessionRefreshError,
)
from app.connectors.aws.session import (
    AWSSessionManager,
    CallerIdentity,
    create_session_manager,
    create_session_manager_for_account,
)

__all__ = [
    "AWSAccountMismatchError",
    "AWSAuthenticationError",
    "AWSConnector",
    "AWSReadOnlyViolationError",
    "AWSRegionNotConfiguredError",
    "AWSSessionManager",
    "AWSSessionRefreshError",
    "CallerIdentity",
    "connector_from_account",
    "create_aws_connectors",
    "create_session_manager",
    "create_session_manager_for_account",
    "get_aws_connector",
    "get_default_aws_connector",
]
