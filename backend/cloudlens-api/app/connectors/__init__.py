"""Cloud provider connectors (session/client factories).

Account targets come from external YAML via CLOUDLENS_CONFIG_PATH.
Session management uses Boto3 + STS (temporary credentials only for AssumeRole).
"""

from app.connectors.aws.client import (
    AWSConnector,
    create_aws_connectors,
    get_aws_connector,
    get_default_aws_connector,
)
from app.connectors.aws.session import (
    AWSSessionManager,
    create_session_manager,
    create_session_manager_for_account,
)

__all__ = [
    "AWSConnector",
    "AWSSessionManager",
    "create_aws_connectors",
    "create_session_manager",
    "create_session_manager_for_account",
    "get_aws_connector",
    "get_default_aws_connector",
]
