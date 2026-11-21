"""CloudFront distribution collector.

CloudFront is global: collection only runs when the orchestration region equals
the configured ``global_control_region``. Origins are recorded as configured
domain names; mapping an origin domain to an inventoried resource (ALB, S3,
API Gateway) is done by the graph builder by exact DNS match, not here.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.collectors.aws_api import AwsDescribeCollector
from app.collectors.normalized import (
    ANY_RESOURCE_TYPE,
    NormalizedResource,
    ResourceEdge,
    ResourcePage,
)
from app.connectors.aws.session import AWSSessionManager


def _items(container: Any) -> list[Any]:
    if isinstance(container, Mapping):
        return list(container.get("Items") or [])
    return []


def _origin_type(origin: Mapping[str, Any]) -> str:
    if origin.get("S3OriginConfig") is not None:
        return "s3"
    if origin.get("VpcOriginConfig") is not None:
        return "vpc"
    return "custom"


class CloudFrontDistributionCollector(AwsDescribeCollector):
    name = "cloudfront.distribution"
    resource_type = "cloudfront-distribution"
    describe_method = "list_distributions"
    result_key = "DistributionList"
    source = "aws.cloudfront"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        cloudfront_service_name: str,
        global_control_region: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            session_manager,
            service_name=cloudfront_service_name,
            require_configured_region=False,
            **kwargs,
        )
        self.source = "aws.cloudfront"
        self._global_control_region = global_control_region.strip()

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        if region != self._global_control_region:
            return ResourcePage(items=[], next_token=None)
        client = self._get_client(self._global_control_region)
        # CloudFront MaxItems is a string-typed parameter.
        params: dict[str, Any] = {"MaxItems": str(min(self._page_size, 100))}
        if next_token:
            params["Marker"] = next_token
        response = self._invoke(
            client, "list_distributions", params, region=self._global_control_region
        )
        dist_list = response.get("DistributionList") or {}
        items = [
            self.normalize(raw, account_id=account_id, region=self._global_control_region)
            for raw in _items(dist_list)
            if isinstance(raw, Mapping) and raw.get("Id")
        ]
        token = None
        if isinstance(dist_list, Mapping) and dist_list.get("IsTruncated"):
            token = dist_list.get("NextMarker")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        dist_id = str(raw["Id"])
        arn = raw.get("ARN") or f"arn:aws:cloudfront::{account_id}:distribution/{dist_id}"
        origins = [
            {
                "id": o.get("Id"),
                "domain_name": o.get("DomainName"),
                "origin_path": o.get("OriginPath") or "",
                "origin_type": _origin_type(o),
            }
            for o in _items(raw.get("Origins"))
            if isinstance(o, Mapping)
        ]
        default_behavior = raw.get("DefaultCacheBehavior") or {}
        web_acl_id = raw.get("WebACLId") or None
        relationships: list[ResourceEdge] = []
        if isinstance(web_acl_id, str) and web_acl_id.startswith("arn:"):
            relationships.append(
                ResourceEdge(
                    relationship_type="uses",
                    to_account_id=account_id,
                    to_region=region,
                    to_service="*",
                    to_resource_type=ANY_RESOURCE_TYPE,
                    to_resource_id=web_acl_id,
                    to_arn=web_acl_id,
                    evidence="ListDistributions:WebACLId",
                )
            )
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=dist_id,
            arn=str(arn),
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=dist_id,
                name=raw.get("Comment") or raw.get("DomainName"),
                status=raw.get("Status"),
                distribution_id=dist_id,
                domain_name=raw.get("DomainName"),
                aliases=[str(a) for a in _items(raw.get("Aliases")) if a],
                enabled=raw.get("Enabled"),
                origins=origins,
                default_origin_id=default_behavior.get("TargetOriginId")
                if isinstance(default_behavior, Mapping)
                else None,
                web_acl_id=web_acl_id,
                price_class=raw.get("PriceClass"),
                http_version=raw.get("HttpVersion"),
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )
