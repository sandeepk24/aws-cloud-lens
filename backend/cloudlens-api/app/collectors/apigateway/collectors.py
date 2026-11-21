"""API Gateway collectors: REST APIs (v1) and HTTP/WebSocket APIs (v2).

Integration targets are recorded only when the integration URI names an ARN
(Lambda function, or an ELB listener for v2 private integrations). Plain HTTP
endpoints are kept as configuration and never mapped to resources here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from app.collectors.aws_api import AwsDescribeCollector
from app.collectors.normalized import (
    ANY_RESOURCE_TYPE,
    NormalizedResource,
    ResourceEdge,
    ResourcePage,
)
from app.connectors.aws.session import AWSSessionManager

# arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/<lambda-arn>/invocations
_LAMBDA_PATH_URI = re.compile(
    r"^arn:[\w-]+:apigateway:[^:]+:lambda:path/[^/]+/functions/(arn:[^/]+)/invocations$"
)


def lambda_arn_from_integration_uri(uri: Any) -> str | None:
    """Return the Lambda function ARN named by an integration URI, else None."""
    if not isinstance(uri, str) or not uri:
        return None
    match = _LAMBDA_PATH_URI.match(uri)
    if match:
        return match.group(1)
    if uri.startswith("arn:") and ":lambda:" in uri and ":function:" in uri:
        return uri
    return None


def _partition(region: str) -> tuple[str, str]:
    """(ARN partition, DNS suffix) for a region code."""
    if region.startswith("cn-"):
        return "aws-cn", "amazonaws.com.cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov", "amazonaws.com"
    return "aws", "amazonaws.com"


def _target_edge(uri: str, *, account_id: str, region: str, evidence: str) -> ResourceEdge | None:
    target = lambda_arn_from_integration_uri(uri)
    if target is None and uri.startswith("arn:") and ":elasticloadbalancing:" in uri:
        target = uri  # v2 private integration to an ALB/NLB listener
    if target is None:
        return None
    return ResourceEdge(
        relationship_type="integrates_with",
        to_account_id=account_id,
        to_region=region,
        to_service="*",
        to_resource_type=ANY_RESOURCE_TYPE,
        to_resource_id=target,
        to_arn=target,
        evidence=evidence,
    )


class ApiGatewayRestApiCollector(AwsDescribeCollector):
    """REST APIs via GetRestApis (position) + GetResources(embed=methods)."""

    name = "apigateway.rest_api"
    resource_type = "apigateway-rest-api"
    describe_method = "get_rest_apis"
    result_key = "items"
    source = "aws.apigateway"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        apigateway_service_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=apigateway_service_name, **kwargs)
        self.source = "aws.apigateway"

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        client = self._get_client(region)
        params: dict[str, Any] = {"limit": min(self._page_size, 500)}
        if next_token:
            params["position"] = next_token
        response = self._invoke(client, "get_rest_apis", params, region=region)
        items: list[NormalizedResource] = []
        for raw in response.get("items") or []:
            if not isinstance(raw, Mapping) or not raw.get("id"):
                continue
            resources = self._resources(client, str(raw["id"]), region=region)
            items.append(
                self._normalize_api(raw, resources=resources, account_id=account_id, region=region)
            )
        token = response.get("position")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def _resources(
        self, client: Any, api_id: str, *, region: str
    ) -> list[Mapping[str, Any]] | None:
        found: list[Mapping[str, Any]] = []
        position: str | None = None
        while True:
            params: dict[str, Any] = {"restApiId": api_id, "limit": 500, "embed": ["methods"]}
            if position:
                params["position"] = position
            response = self._invoke_soft(
                client, "get_resources", params, region=region, context=f"rest_api={api_id}"
            )
            if response is None:
                return None if not found else found
            found.extend(r for r in response.get("items") or [] if isinstance(r, Mapping))
            position = response.get("position")
            if not position:
                return found

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        return self._normalize_api(raw, resources=[], account_id=account_id, region=region)

    def _normalize_api(
        self,
        raw: Mapping[str, Any],
        *,
        resources: list[Mapping[str, Any]] | None,
        account_id: str,
        region: str,
    ) -> NormalizedResource:
        api_id = str(raw["id"])
        partition, suffix = _partition(region)
        arn = f"arn:{partition}:apigateway:{region}::/restapis/{api_id}"
        integrations: list[dict[str, Any]] = []
        relationships: list[ResourceEdge] = []
        seen: set[str] = set()
        for resource in resources or []:
            for method, spec in (resource.get("resourceMethods") or {}).items():
                if not isinstance(spec, Mapping):
                    continue
                integ = spec.get("methodIntegration") or {}
                if not isinstance(integ, Mapping) or not integ:
                    continue
                uri = integ.get("uri")
                integrations.append(
                    {
                        "path": resource.get("path"),
                        "method": method,
                        "type": integ.get("type"),
                        "uri": uri,
                        "connection_type": integ.get("connectionType"),
                        "connection_id": integ.get("connectionId"),
                    }
                )
                if isinstance(uri, str):
                    edge = _target_edge(
                        uri,
                        account_id=account_id,
                        region=region,
                        evidence="GetResources:resourceMethods.methodIntegration.uri",
                    )
                    if edge is not None and edge.to_resource_id not in seen:
                        seen.add(edge.to_resource_id)
                        relationships.append(edge)
        endpoint_cfg = raw.get("endpointConfiguration") or {}
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=api_id,
            arn=arn,
            tags={str(k): str(v) for k, v in (raw.get("tags") or {}).items()},
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=api_id,
                name=raw.get("name"),
                status="available",
                api_id=api_id,
                description=raw.get("description"),
                endpoint_types=list(endpoint_cfg.get("types") or [])
                if isinstance(endpoint_cfg, Mapping)
                else [],
                endpoint_domains=[f"{api_id}.execute-api.{region}.{suffix}"],
                integrations=integrations,
                integrations_collected=resources is not None,
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )


class ApiGatewayV2ApiCollector(AwsDescribeCollector):
    """HTTP / WebSocket APIs via GetApis + GetIntegrations (NextToken)."""

    name = "apigatewayv2.api"
    resource_type = "apigatewayv2-api"
    describe_method = "get_apis"
    result_key = "Items"
    source = "aws.apigatewayv2"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        apigatewayv2_service_name: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=apigatewayv2_service_name, **kwargs)
        self.source = "aws.apigatewayv2"

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        client = self._get_client(region)
        # apigatewayv2 MaxResults is a string-typed parameter.
        params: dict[str, Any] = {"MaxResults": str(min(self._page_size, 500))}
        if next_token:
            params["NextToken"] = next_token
        response = self._invoke(client, "get_apis", params, region=region)
        items: list[NormalizedResource] = []
        for raw in response.get("Items") or []:
            if not isinstance(raw, Mapping) or not raw.get("ApiId"):
                continue
            integrations = self._integrations(client, str(raw["ApiId"]), region=region)
            items.append(
                self._normalize_api(
                    raw, integrations=integrations, account_id=account_id, region=region
                )
            )
        token = response.get("NextToken")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def _integrations(
        self, client: Any, api_id: str, *, region: str
    ) -> list[Mapping[str, Any]] | None:
        found: list[Mapping[str, Any]] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"ApiId": api_id, "MaxResults": "500"}
            if token:
                params["NextToken"] = token
            response = self._invoke_soft(
                client, "get_integrations", params, region=region, context=f"api={api_id}"
            )
            if response is None:
                return None if not found else found
            found.extend(i for i in response.get("Items") or [] if isinstance(i, Mapping))
            token = response.get("NextToken")
            if not token:
                return found

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        return self._normalize_api(raw, integrations=[], account_id=account_id, region=region)

    def _normalize_api(
        self,
        raw: Mapping[str, Any],
        *,
        integrations: list[Mapping[str, Any]] | None,
        account_id: str,
        region: str,
    ) -> NormalizedResource:
        api_id = str(raw["ApiId"])
        partition, _suffix = _partition(region)
        arn = f"arn:{partition}:apigateway:{region}::/apis/{api_id}"
        endpoint = raw.get("ApiEndpoint")
        host = urlparse(str(endpoint)).hostname if endpoint else None
        recorded: list[dict[str, Any]] = []
        relationships: list[ResourceEdge] = []
        seen: set[str] = set()
        for integ in integrations or []:
            uri = integ.get("IntegrationUri")
            recorded.append(
                {
                    "integration_id": integ.get("IntegrationId"),
                    "type": integ.get("IntegrationType"),
                    "uri": uri,
                    "connection_type": integ.get("ConnectionType"),
                    "connection_id": integ.get("ConnectionId"),
                }
            )
            if isinstance(uri, str):
                edge = _target_edge(
                    uri,
                    account_id=account_id,
                    region=region,
                    evidence="GetIntegrations:IntegrationUri",
                )
                if edge is not None and edge.to_resource_id not in seen:
                    seen.add(edge.to_resource_id)
                    relationships.append(edge)
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=api_id,
            arn=arn,
            tags={str(k): str(v) for k, v in (raw.get("Tags") or {}).items()},
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=api_id,
                name=raw.get("Name"),
                status="available",
                api_id=api_id,
                protocol_type=raw.get("ProtocolType"),
                api_endpoint=endpoint,
                endpoint_domains=[host] if host else [],
                disable_execute_api_endpoint=raw.get("DisableExecuteApiEndpoint"),
                integrations=recorded,
                integrations_collected=integrations is not None,
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )
