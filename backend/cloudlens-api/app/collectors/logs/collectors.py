"""CloudWatch Logs collectors — log group metadata only (never log events)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.collectors.aws_api import AwsDescribeCollector
from app.collectors.base import PaginatedCollector
from app.collectors.normalized import NormalizedResource
from app.connectors.aws.session import AWSSessionManager


def _ms_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value)


class CloudWatchLogGroupCollector(AwsDescribeCollector):
    """Collect CloudWatch log group configuration metadata.

    Does not call ``get_log_events``, ``filter_log_events``, or Logs Insights.
    """

    name = "logs.log_group"
    resource_type = "logs-log-group"
    describe_method = "describe_log_groups"
    result_key = "logGroups"
    token_param = "nextToken"
    token_response_key = "nextToken"
    page_size_param = "limit"
    source = "aws.logs"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        logs_service_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=logs_service_name, **kwargs)
        self.source = "aws.logs"

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        name = str(raw.get("logGroupName") or "")
        arn = raw.get("arn") or raw.get("logGroupArn")
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=name,
            arn=str(arn) if arn else None,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=name,
                name=name,
                status="available",
                retention_in_days=raw.get("retentionInDays"),
                stored_bytes=raw.get("storedBytes"),
                metric_filter_count=raw.get("metricFilterCount"),
                # KMS key id/arn only — never key material or plaintext.
                kms_key_id=raw.get("kmsKeyId"),
                data_protection_status=raw.get("dataProtectionStatus"),
                inherited_properties=raw.get("inheritedProperties") or [],
                creation_time=_ms_to_iso(raw.get("creationTime")),
                discovery_timestamp=datetime.now(UTC).isoformat(),
                log_events_collected=False,
            ),
        )


def create_logs_collectors(
    session_manager: AWSSessionManager,
    *,
    logs_service_name: str,
    page_size: int = 50,
    max_retries: int = 5,
) -> list[PaginatedCollector]:
    return [
        CloudWatchLogGroupCollector(
            session_manager,
            logs_service_name=logs_service_name,
            page_size=page_size,
            max_retries=max_retries,
        )
    ]
