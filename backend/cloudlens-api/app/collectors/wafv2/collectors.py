"""WAFv2 Web ACL collectors — regional and CloudFront scopes.

CloudFront-scope WAFs are only inventoried when the collection region equals the
configured ``global_control_region`` (required by the WAFv2 API).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from app.collectors.aws_api import AwsDescribeCollector
from app.collectors.base import PaginatedCollector
from app.collectors.normalized import (
    ANY_RESOURCE_TYPE,
    NormalizedResource,
    ResourceEdge,
    ResourcePage,
)
from app.connectors.aws.session import AWSSessionManager

WafScope = Literal["REGIONAL", "CLOUDFRONT"]


class _Wafv2WebAclCollectorBase(AwsDescribeCollector):
    """Shared list/get Web ACL + association logic."""

    resource_type = "wafv2-web-acl"
    describe_method = "list_web_acls"
    result_key = "WebACLs"
    source = "aws.wafv2"
    scope: WafScope

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        wafv2_service_name: str,
        global_control_region: str,
        scope: WafScope,
        **kwargs: Any,
    ) -> None:
        require = scope != "CLOUDFRONT"
        super().__init__(
            session_manager,
            service_name=wafv2_service_name,
            require_configured_region=require,
            **kwargs,
        )
        self.source = "aws.wafv2"
        self.scope = scope
        self._global_control_region = global_control_region.strip()

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        api_region = self._api_region(region)
        if api_region is None:
            return ResourcePage(items=[], next_token=None)

        client = self._get_client(api_region)
        params: dict[str, Any] = {
            "Scope": self.scope,
            "Limit": min(self._page_size, 100),
        }
        if next_token:
            params["NextMarker"] = next_token
        response = self._invoke(client, "list_web_acls", params, region=api_region)
        items: list[NormalizedResource] = []
        for summary in response.get("WebACLs") or []:
            if not isinstance(summary, Mapping):
                continue
            name = summary.get("Name")
            acl_id = summary.get("Id")
            if not name or not acl_id:
                continue
            detail = self._invoke_soft(
                client,
                "get_web_acl",
                {"Name": name, "Id": acl_id, "Scope": self.scope},
                region=api_region,
                context=f"webacl={name}",
            )
            associations = self._list_associations(
                client, web_acl_arn=str(summary.get("ARN") or ""), region=api_region
            )
            items.append(
                self._normalize_acl(
                    summary=summary,
                    detail=detail,
                    associations=associations,
                    account_id=account_id,
                    region=api_region if self.scope == "CLOUDFRONT" else region,
                )
            )
        token = response.get("NextMarker")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        return self._normalize_acl(
            summary=raw,
            detail=None,
            associations=[],
            account_id=account_id,
            region=region,
        )

    def _api_region(self, collection_region: str) -> str | None:
        if self.scope == "REGIONAL":
            return collection_region
        # CLOUDFRONT scope must be queried from the configured global control region.
        if collection_region == self._global_control_region:
            return self._global_control_region
        return None

    # WAFv2 ListResourcesForWebACL ResourceType values for regional scope.
    _REGIONAL_ASSOCIATION_TYPES = ("APPLICATION_LOAD_BALANCER", "API_GATEWAY")

    def _list_associations(
        self, client: Any, *, web_acl_arn: str, region: str
    ) -> list[str]:
        """Resources associated with a regional Web ACL.

        CloudFront-scope ACLs are not listed here: AWS documents that
        ListResourcesForWebACL must not be used for CloudFront; the association
        is read from each distribution's ``WebACLId`` instead.
        """
        if not web_acl_arn or self.scope != "REGIONAL":
            return []
        found: list[str] = []
        for resource_type in self._REGIONAL_ASSOCIATION_TYPES:
            response = self._invoke_soft(
                client,
                "list_resources_for_web_acl",
                {"WebACLArn": web_acl_arn, "ResourceType": resource_type},
                region=region,
                context=f"associations={web_acl_arn} type={resource_type}",
            )
            if response is None:
                continue
            for arn in response.get("ResourceArns") or []:
                if arn and str(arn) not in found:
                    found.append(str(arn))
        return found

    def _normalize_acl(
        self,
        *,
        summary: Mapping[str, Any],
        detail: Mapping[str, Any] | None,
        associations: list[str],
        account_id: str,
        region: str,
    ) -> NormalizedResource:
        name = str(summary.get("Name") or "")
        acl_id = str(summary.get("Id") or "")
        arn = summary.get("ARN")
        web_acl = (detail or {}).get("WebACL") if isinstance(detail, Mapping) else None
        if not isinstance(web_acl, Mapping):
            web_acl = {}
        rules = []
        for rule in web_acl.get("Rules") or []:
            if not isinstance(rule, Mapping):
                continue
            # Rule structure metadata only — no request bodies / secrets.
            rules.append(
                {
                    "name": rule.get("Name"),
                    "priority": rule.get("Priority"),
                    "action": list((rule.get("Action") or {}).keys())
                    or list((rule.get("OverrideAction") or {}).keys()),
                    "statement_types": list((rule.get("Statement") or {}).keys()),
                }
            )
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=arn or f"{self.scope}:{name}:{acl_id}",
            arn=str(arn) if arn else None,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=str(arn or name),
                name=name,
                status="available",
                scope=self.scope,
                web_acl_id=acl_id,
                description=web_acl.get("Description"),
                capacity=web_acl.get("Capacity"),
                managed_by_firewall_manager=web_acl.get("ManagedByFirewallManager"),
                default_action=list((web_acl.get("DefaultAction") or {}).keys()),
                rules=rules,
                associated_resource_arns=associations,
                association_count=len(associations),
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
            relationships=[
                ResourceEdge(
                    relationship_type="protects",
                    to_account_id=account_id,
                    to_region=region,
                    to_service="*",
                    to_resource_type=ANY_RESOURCE_TYPE,
                    to_resource_id=resource_arn,
                    to_arn=resource_arn,
                    evidence="ListResourcesForWebACL:ResourceArns",
                )
                for resource_arn in associations
            ],
        )


class Wafv2RegionalWebAclCollector(_Wafv2WebAclCollectorBase):
    name = "wafv2.web_acl_regional"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        wafv2_service_name: str,
        global_control_region: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            session_manager,
            wafv2_service_name=wafv2_service_name,
            global_control_region=global_control_region,
            scope="REGIONAL",
            **kwargs,
        )


class Wafv2CloudFrontWebAclCollector(_Wafv2WebAclCollectorBase):
    name = "wafv2.web_acl_cloudfront"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        wafv2_service_name: str,
        global_control_region: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            session_manager,
            wafv2_service_name=wafv2_service_name,
            global_control_region=global_control_region,
            scope="CLOUDFRONT",
            **kwargs,
        )


def create_wafv2_collectors(
    session_manager: AWSSessionManager,
    *,
    wafv2_service_name: str,
    global_control_region: str,
    page_size: int = 100,
    max_retries: int = 5,
) -> list[PaginatedCollector]:
    common = {
        "wafv2_service_name": wafv2_service_name,
        "global_control_region": global_control_region,
        "page_size": page_size,
        "max_retries": max_retries,
    }
    return [
        Wafv2RegionalWebAclCollector(session_manager, **common),
        Wafv2CloudFrontWebAclCollector(session_manager, **common),
    ]
