"""Shared AWS Describe* helpers for paginated and batched collectors."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.collectors.base import PaginatedCollector

# Re-export tag helpers used across collector packages.
from app.collectors.network.common import edge as edge
from app.collectors.network.common import tag_name as tag_name
from app.collectors.network.common import tags_to_dict as tags_to_dict
from app.collectors.normalized import (
    CollectorError,
    NormalizedResource,
    ResourceEdge,
    ResourcePage,
)
from app.connectors.aws.retry import call_with_backoff, is_retryable_aws_error
from app.connectors.aws.session import AWSSessionManager

logger = logging.getLogger(__name__)

ClientFactory = Callable[[str], Any]

# Access / not-found errors that should not abort a multi-resource collection page.
SOFT_FAILURE_ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedOperation",
        "UnauthorizedException",
        "ResourceNotFoundException",
        "ClusterNotFoundException",
        "ServiceNotFoundException",
        "InvalidParameterException",
        "ClientException",
        "NoSuchBucket",
        "NoSuchBucketPolicy",
        "ServerSideEncryptionConfigurationNotFoundError",
        "OwnershipControlsNotFoundError",
        "NoSuchPublicAccessBlockConfiguration",
        "NoSuchEntity",
        "WAFNonexistentItemException",
        "WAFInvalidParameterException",
    }
)


def chunked(values: Sequence[str], size: int) -> list[list[str]]:
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    return [list(values[i : i + size]) for i in range(0, len(values), size)]


def compact_edges(*edges: ResourceEdge | None) -> list[ResourceEdge]:
    return [item for item in edges if item is not None]


def is_soft_failure(exc: BaseException) -> bool:
    if isinstance(exc, CollectorError):
        return bool(exc.error_code and exc.error_code in SOFT_FAILURE_ERROR_CODES)
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in SOFT_FAILURE_ERROR_CODES
    return False


class AwsDescribeCollector(PaginatedCollector):
    """Base collector for paginated AWS describe APIs with throttling retries."""

    version = "0.1.0"
    source = "aws"
    name: str
    resource_type: str
    describe_method: str
    result_key: str
    # EC2-style NextToken vs ELBv2-style Marker
    token_param: str = "NextToken"
    token_response_key: str = "NextToken"
    page_size_param: str = "MaxResults"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        service_name: str,
        page_size: int = 100,
        max_retries: int = 5,
        client_factory: ClientFactory | None = None,
        sleep: Callable[[float], None] | None = None,
        require_configured_region: bool = True,
    ) -> None:
        if not service_name.strip():
            raise ValueError("service_name must be provided from configuration")
        if page_size < 1 or page_size > 1000:
            raise ValueError("page_size must be between 1 and 1000")
        self._session_manager = session_manager
        self._service_name = service_name.strip()
        self.service = self._service_name
        self._page_size = page_size
        self._max_retries = max_retries
        self._client_factory = client_factory
        self._sleep = sleep
        self._require_configured_region = require_configured_region
        # Per-item access/not-found failures skipped by ``_invoke_soft``; the
        # orchestrator drains them per region and records them as collection
        # errors so coverage reports what could not be read.
        self._soft_failures: list[CollectorError] = []
        self._soft_lock = threading.Lock()

    def drain_soft_failures(self, region: str) -> list[CollectorError]:
        with self._soft_lock:
            mine = [f for f in self._soft_failures if f.region == region]
            self._soft_failures = [f for f in self._soft_failures if f.region != region]
        return mine

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        client = self._get_client(region)
        params: dict[str, Any] = {self.page_size_param: self._page_size}
        if next_token:
            params[self.token_param] = next_token
        response = self._invoke(client, self.describe_method, params, region=region)
        raw_items = response.get(self.result_key) or []
        items = [
            self.normalize(raw, account_id=account_id, region=region)
            for raw in raw_items
            if isinstance(raw, Mapping)
        ]
        token = response.get(self.token_response_key)
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
        return self._session_manager.client(
            self._service_name,
            region=region,
            require_configured_region=self._require_configured_region,
        )

    def _invoke(
        self,
        client: Any,
        method_name: str,
        params: dict[str, Any],
        *,
        region: str,
    ) -> Mapping[str, Any]:
        method = getattr(client, method_name)
        kwargs: dict[str, Any] = {
            "max_attempts": self._max_retries,
            "is_retryable": is_retryable_aws_error,
        }
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        try:
            return call_with_backoff(lambda: method(**params), **kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            message = exc.response.get("Error", {}).get("Message", str(exc))
            raise CollectorError(
                message=f"{method_name} failed: {code}: {message}",
                error_code=str(code),
                retryable=is_retryable_aws_error(exc),
                region=region,
                details={"method": method_name},
            ) from exc
        except BotoCoreError as exc:
            raise CollectorError(
                message=f"{method_name} failed: {exc}",
                error_code="BotoCoreError",
                retryable=True,
                region=region,
                details={"method": method_name},
            ) from exc

    def _invoke_soft(
        self,
        client: Any,
        method_name: str,
        params: dict[str, Any],
        *,
        region: str,
        context: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Like ``_invoke``, but access/not-found failures return ``None``.

        Used so one denied cluster/service does not abort the whole page/run.
        """
        try:
            return self._invoke(client, method_name, params, region=region)
        except CollectorError as exc:
            if is_soft_failure(exc):
                with self._soft_lock:
                    if len(self._soft_failures) < 1000:
                        exc.details = {**exc.details, "context": context, "soft": True}
                        self._soft_failures.append(exc)
                logger.warning(
                    "Skipping %s after soft failure (%s) region=%s context=%s: %s",
                    method_name,
                    exc.error_code,
                    region,
                    context,
                    exc,
                )
                return None
            raise

    def _base_state(
        self,
        *,
        account_id: str,
        region: str,
        resource_id: str,
        name: str | None = None,
        status: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "account_id": account_id,
            "region": region,
            "resource_id": resource_id,
        }
        if name:
            state["name"] = name
        if status is not None:
            state["status"] = status
        state.update(extra)
        return state
