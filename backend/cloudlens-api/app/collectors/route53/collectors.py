"""Route 53 collectors — hosted zones, plus alias/CNAME records only.

Route 53 is a global service. Collection only runs when the orchestration region
equals the configured ``global_control_region``. Record counts come from
``GetHostedZone`` / zone metadata — record values are never fetched.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.collectors.aws_api import AwsDescribeCollector
from app.collectors.base import PaginatedCollector
from app.collectors.normalized import NormalizedResource, ResourceEdge, ResourcePage
from app.connectors.aws.session import AWSSessionManager


class Route53HostedZoneCollector(AwsDescribeCollector):
    """Collect Route 53 hosted zone configuration and resource-record-set counts."""

    name = "route53.hosted_zone"
    resource_type = "route53-hosted-zone"
    describe_method = "list_hosted_zones"
    result_key = "HostedZones"
    source = "aws.route53"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        route53_service_name: str,
        global_control_region: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            session_manager,
            service_name=route53_service_name,
            require_configured_region=False,
            **kwargs,
        )
        self.source = "aws.route53"
        self._global_control_region = global_control_region.strip()

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        if region != self._global_control_region:
            # Avoid duplicating global inventory in every regional run.
            return ResourcePage(items=[], next_token=None)

        client = self._get_client(self._global_control_region)
        params: dict[str, Any] = {"MaxItems": str(min(self._page_size, 100))}
        if next_token:
            params["Marker"] = next_token
        response = self._invoke(
            client, "list_hosted_zones", params, region=self._global_control_region
        )
        items: list[NormalizedResource] = []
        for zone in response.get("HostedZones") or []:
            if not isinstance(zone, Mapping):
                continue
            zone_id = str(zone.get("Id") or "")
            detail = None
            if zone_id:
                detail = self._invoke_soft(
                    client,
                    "get_hosted_zone",
                    {"Id": zone_id},
                    region=self._global_control_region,
                    context=f"hosted_zone={zone_id}",
                )
            items.append(
                self._normalize_zone(
                    zone,
                    detail=detail,
                    account_id=account_id,
                    region=self._global_control_region,
                )
            )
        token = None
        if response.get("IsTruncated"):
            token = response.get("NextMarker") or response.get("Marker")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        return self._normalize_zone(raw, detail=None, account_id=account_id, region=region)

    def _normalize_zone(
        self,
        zone: Mapping[str, Any],
        *,
        detail: Mapping[str, Any] | None,
        account_id: str,
        region: str,
    ) -> NormalizedResource:
        zone_id = str(zone.get("Id") or "")
        name = str(zone.get("Name") or "").rstrip(".")
        # Prefer authoritative count from GetHostedZone when available.
        record_count = zone.get("ResourceRecordSetCount")
        config = zone.get("Config") or {}
        if isinstance(detail, Mapping):
            detailed_zone = detail.get("HostedZone") or {}
            if isinstance(detailed_zone, Mapping):
                record_count = detailed_zone.get("ResourceRecordSetCount", record_count)
                config = detailed_zone.get("Config") or config
        # Never list ResourceRecordSets — that would expose record values.
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=zone_id or name,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=zone_id or name,
                name=name,
                status="available",
                hosted_zone_id=zone_id,
                private_zone=(
                    config.get("PrivateZone") if isinstance(config, Mapping) else None
                ),
                comment=config.get("Comment") if isinstance(config, Mapping) else None,
                resource_record_set_count=record_count,
                caller_reference=zone.get("CallerReference"),
                discovery_timestamp=datetime.now(UTC).isoformat(),
                record_values_collected=False,
            ),
        )


def create_route53_collectors(
    session_manager: AWSSessionManager,
    *,
    route53_service_name: str,
    global_control_region: str,
    page_size: int = 100,
    max_retries: int = 5,
) -> list[PaginatedCollector]:
    return [
        Route53HostedZoneCollector(
            session_manager,
            route53_service_name=route53_service_name,
            global_control_region=global_control_region,
            page_size=page_size,
            max_retries=max_retries,
        )
    ]


def normalize_dns_name(name: object) -> str | None:
    """Lower-case, strip the trailing dot and the ELB ``dualstack.`` prefix."""
    if not isinstance(name, str) or not name.strip():
        return None
    value = name.strip().lower().rstrip(".")
    if value.startswith("dualstack."):
        value = value[len("dualstack.") :]
    return value


class Route53RecordCollector(AwsDescribeCollector):
    """Collect alias (A/AAAA) and CNAME records for every hosted zone.

    Only records that can point at AWS resources are kept; other record values
    are never stored. Mapping an alias DNS name to an inventoried resource is
    done by the graph builder (exact match of normalized DNS names).
    """

    name = "route53.record"
    resource_type = "route53-record"
    describe_method = "list_resource_record_sets"
    result_key = "ResourceRecordSets"
    source = "aws.route53"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        route53_service_name: str,
        global_control_region: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            session_manager,
            service_name=route53_service_name,
            require_configured_region=False,
            **kwargs,
        )
        self.source = "aws.route53"
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
        api_region = self._global_control_region
        client = self._get_client(api_region)
        params: dict[str, Any] = {"MaxItems": str(min(self._page_size, 100))}
        if next_token:
            params["Marker"] = next_token
        zones = self._invoke(client, "list_hosted_zones", params, region=api_region)
        items: list[NormalizedResource] = []
        for zone in zones.get("HostedZones") or []:
            if not isinstance(zone, Mapping) or not zone.get("Id"):
                continue
            zone_id = str(zone["Id"])
            for record in self._records(client, zone_id, region=api_region):
                items.append(
                    self._normalize_record(
                        record, zone_id=zone_id, account_id=account_id, region=api_region
                    )
                )
        token = zones.get("NextMarker") if zones.get("IsTruncated") else None
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def _records(self, client: Any, zone_id: str, *, region: str) -> list[Mapping[str, Any]]:
        kept: list[Mapping[str, Any]] = []
        start: dict[str, Any] = {}
        while True:
            params: dict[str, Any] = {"HostedZoneId": zone_id, "MaxItems": "300", **start}
            response = self._invoke_soft(
                client, "list_resource_record_sets", params, region=region,
                context=f"records zone={zone_id}",
            )
            if response is None:
                return kept
            for record in response.get("ResourceRecordSets") or []:
                if not isinstance(record, Mapping):
                    continue
                rtype = record.get("Type")
                if record.get("AliasTarget") and rtype in {"A", "AAAA"}:
                    kept.append(record)
                elif rtype == "CNAME":
                    kept.append(record)
            if not response.get("IsTruncated"):
                return kept
            start = {"StartRecordName": response.get("NextRecordName")}
            if response.get("NextRecordType"):
                start["StartRecordType"] = response["NextRecordType"]
            if response.get("NextRecordIdentifier"):
                start["StartRecordIdentifier"] = response["NextRecordIdentifier"]
            if not start["StartRecordName"]:
                return kept

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        return self._normalize_record(raw, zone_id="", account_id=account_id, region=region)

    def _normalize_record(
        self,
        record: Mapping[str, Any],
        *,
        zone_id: str,
        account_id: str,
        region: str,
    ) -> NormalizedResource:
        name = str(record.get("Name") or "")
        rtype = str(record.get("Type") or "")
        set_id = str(record.get("SetIdentifier") or "")
        resource_id = f"{zone_id}/{name}/{rtype}/{set_id}"
        alias = record.get("AliasTarget")
        alias_target = None
        if isinstance(alias, Mapping):
            alias_target = {
                "dns_name": alias.get("DNSName"),
                "dns_name_normalized": normalize_dns_name(alias.get("DNSName")),
                "hosted_zone_id": alias.get("HostedZoneId"),
                "evaluate_target_health": alias.get("EvaluateTargetHealth"),
            }
        cname_values = (
            [
                str(v.get("Value"))
                for v in record.get("ResourceRecords") or []
                if isinstance(v, Mapping) and v.get("Value")
            ]
            if rtype == "CNAME"
            else []
        )
        relationships = []
        if zone_id:
            relationships.append(
                ResourceEdge(
                    relationship_type="belongs_to",
                    to_account_id=account_id,
                    to_region=region,
                    to_service=self.service,
                    to_resource_type="route53-hosted-zone",
                    to_resource_id=zone_id,
                    evidence="ListResourceRecordSets:HostedZoneId(request)",
                )
            )
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=resource_id,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=resource_id,
                name=name.rstrip("."),
                status="available",
                hosted_zone_id=zone_id,
                record_name=name,
                record_name_normalized=normalize_dns_name(name),
                record_type=rtype,
                set_identifier=set_id or None,
                alias_target=alias_target,
                cname_values=cname_values,
                cname_values_normalized=[normalize_dns_name(v) for v in cname_values],
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=relationships,
        )
