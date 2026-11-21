"""Shared helpers for EC2 network collectors."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.collectors.base import PaginatedCollector
from app.collectors.normalized import (
    CollectorError,
    NormalizedResource,
    ResourceEdge,
    ResourcePage,
)
from app.connectors.aws.retry import call_with_backoff, is_retryable_aws_error
from app.connectors.aws.session import AWSSessionManager


def tags_to_dict(tags: Sequence[Mapping[str, Any]] | None) -> dict[str, str]:
    """Convert EC2 ``[{Key, Value}, ...]`` tags to a flat mapping."""
    if not tags:
        return {}
    result: dict[str, str] = {}
    for tag in tags:
        key = tag.get("Key")
        if key is None:
            continue
        value = tag.get("Value")
        result[str(key)] = "" if value is None else str(value)
    return result


def tag_name(tags: dict[str, str]) -> str | None:
    return tags.get("Name")


def edge(
    *,
    relationship_type: str,
    account_id: str,
    region: str,
    service: str,
    resource_type: str,
    resource_id: str | None,
) -> ResourceEdge | None:
    """Build a relationship edge only when the target ID is verified present."""
    if not resource_id or not str(resource_id).strip():
        return None
    return ResourceEdge(
        relationship_type=relationship_type,
        to_account_id=account_id,
        to_region=region,
        to_service=service,
        to_resource_type=resource_type,
        to_resource_id=str(resource_id).strip(),
    )


ClientFactory = Callable[[str], Any]


class Ec2NetworkCollector(PaginatedCollector):
    """Paginated EC2 Describe* collector with throttling-aware retries."""

    version = "0.1.0"
    source = "aws.ec2"

    # Subclasses set these.
    name: str
    resource_type: str
    describe_method: str
    result_key: str

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        ec2_service_name: str,
        page_size: int = 100,
        max_retries: int = 5,
        client_factory: ClientFactory | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not ec2_service_name.strip():
            raise ValueError("ec2_service_name must be provided from configuration")
        if page_size < 5 or page_size > 1000:
            raise ValueError("page_size must be between 5 and 1000")
        self._session_manager = session_manager
        self._ec2_service_name = ec2_service_name.strip()
        self.service = self._ec2_service_name
        self._page_size = page_size
        self._max_retries = max_retries
        self._client_factory = client_factory
        self._sleep = sleep

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        client = self._get_client(region)
        params: dict[str, Any] = {"MaxResults": self._page_size}
        if next_token:
            params["NextToken"] = next_token

        try:
            response = self._call_describe(client, params)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            message = exc.response.get("Error", {}).get("Message", str(exc))
            raise CollectorError(
                message=f"{self.describe_method} failed: {code}: {message}",
                error_code=str(code),
                retryable=is_retryable_aws_error(exc),
                region=region,
                details={"method": self.describe_method},
            ) from exc
        except BotoCoreError as exc:
            raise CollectorError(
                message=f"{self.describe_method} failed: {exc}",
                error_code="BotoCoreError",
                retryable=True,
                region=region,
                details={"method": self.describe_method},
            ) from exc

        raw_items = response.get(self.result_key) or []
        items = [
            self.normalize(raw, account_id=account_id, region=region)
            for raw in raw_items
            if isinstance(raw, Mapping)
        ]
        token = response.get("NextToken")
        return ResourcePage(
            items=items,
            next_token=str(token) if token else None,
        )

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        raise NotImplementedError

    def _get_client(self, region: str) -> Any:
        if self._client_factory is not None:
            return self._client_factory(region)
        return self._session_manager.client(self._ec2_service_name, region=region)

    def _call_describe(self, client: Any, params: dict[str, Any]) -> Mapping[str, Any]:
        method = getattr(client, self.describe_method)
        kwargs: dict[str, Any] = {
            "max_attempts": self._max_retries,
            "is_retryable": is_retryable_aws_error,
        }
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        return call_with_backoff(lambda: method(**params), **kwargs)

    def _base_state(
        self,
        *,
        account_id: str,
        region: str,
        resource_id: str,
        name: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "account_id": account_id,
            "region": region,
            "resource_id": resource_id,
        }
        if name:
            state["name"] = name
        state.update(extra)
        return state
