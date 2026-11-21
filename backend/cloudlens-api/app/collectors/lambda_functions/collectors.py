"""Lambda function collector — ListFunctions metadata, never code or env values."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.collectors.aws_api import AwsDescribeCollector
from app.collectors.normalized import NormalizedResource, ResourceEdge
from app.connectors.aws.session import AWSSessionManager


class LambdaFunctionCollector(AwsDescribeCollector):
    """Collect Lambda functions and their VPC placement.

    ``ListFunctions`` pages with ``Marker`` / ``NextMarker`` (MaxItems <= 50).
    Environment variables are deliberately not recorded (they often hold secrets).
    """

    name = "lambda.function"
    resource_type = "lambda-function"
    describe_method = "list_functions"
    result_key = "Functions"
    token_param = "Marker"
    token_response_key = "NextMarker"
    page_size_param = "MaxItems"
    source = "aws.lambda"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        lambda_service_name: str,
        ec2_service_name: str,
        **kwargs: Any,
    ) -> None:
        kwargs["page_size"] = max(1, min(int(kwargs.get("page_size", 50)), 50))
        super().__init__(session_manager, service_name=lambda_service_name, **kwargs)
        self.source = "aws.lambda"
        self._ec2_service_name = ec2_service_name.strip()

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        arn = str(raw["FunctionArn"])
        name = str(raw.get("FunctionName") or arn.rsplit(":", 1)[-1])
        vpc = raw.get("VpcConfig") or {}
        vpc_id = vpc.get("VpcId") if isinstance(vpc, Mapping) else None
        subnet_ids = [str(s) for s in (vpc.get("SubnetIds") or []) if s] if vpc_id else []
        sg_ids = [str(g) for g in (vpc.get("SecurityGroupIds") or []) if g] if vpc_id else []

        def _edge(rel: str, rtype: str, rid: str, evidence: str) -> ResourceEdge:
            return ResourceEdge(
                relationship_type=rel,
                to_account_id=account_id,
                to_region=region,
                to_service=self._ec2_service_name,
                to_resource_type=rtype,
                to_resource_id=rid,
                evidence=evidence,
            )

        relationships: list[ResourceEdge] = []
        if vpc_id:
            relationships.append(
                _edge("belongs_to", "vpc", str(vpc_id), "ListFunctions:VpcConfig.VpcId")
            )
        relationships += [
            _edge("associated_with", "subnet", s, "ListFunctions:VpcConfig.SubnetIds")
            for s in subnet_ids
        ]
        relationships += [
            _edge("uses", "security-group", g, "ListFunctions:VpcConfig.SecurityGroupIds")
            for g in sg_ids
        ]
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=arn,
            arn=arn,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=arn,
                name=name,
                status=raw.get("State"),
                function_arn=arn,
                runtime=raw.get("Runtime"),
                handler=raw.get("Handler"),
                package_type=raw.get("PackageType"),
                architectures=list(raw.get("Architectures") or []),
                memory_size=raw.get("MemorySize"),
                timeout=raw.get("Timeout"),
                last_modified=raw.get("LastModified"),
                role_arn=raw.get("Role"),
                vpc_id=vpc_id,
                subnet_ids=subnet_ids,
                security_group_ids=sg_ids,
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )
