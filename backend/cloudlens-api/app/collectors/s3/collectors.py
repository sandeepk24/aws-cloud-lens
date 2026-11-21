"""S3 bucket collectors — config metadata only; never objects or secret values.

Handles global bucket listing and per-bucket region resolution so regional
orchestration only inventories buckets that reside in the current region.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.collectors.aws_api import AwsDescribeCollector
from app.collectors.base import PaginatedCollector
from app.collectors.normalized import NormalizedResource, ResourcePage
from app.connectors.aws.session import AWSSessionManager


def _parse_dt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.isoformat()
    return str(value)


def resolve_bucket_region(
    location_constraint: str | None,
    *,
    default_region: str,
    alias_map: Mapping[str, str],
) -> str:
    """Map S3 LocationConstraint to a region code using config-driven defaults."""
    if location_constraint is None or str(location_constraint).strip() == "":
        return default_region
    key = str(location_constraint).strip()
    return alias_map.get(key, key)


class S3BucketCollector(AwsDescribeCollector):
    """Collect S3 bucket configuration for buckets in the current region.

    Never calls object-data APIs (``list_objects``, ``get_object``, etc.).
    """

    name = "s3.bucket"
    resource_type = "s3-bucket"
    describe_method = "list_buckets"
    result_key = "Buckets"
    source = "aws.s3"

    def __init__(
        self,
        session_manager: AWSSessionManager,
        *,
        s3_service_name: str,
        s3_default_region: str,
        s3_location_constraint_map: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(session_manager, service_name=s3_service_name, **kwargs)
        self.source = "aws.s3"
        self._s3_default_region = s3_default_region.strip()
        self._location_map = dict(s3_location_constraint_map or {})

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        # list_buckets is account-global; ContinuationToken supports pagination.
        client = self._get_client(region)
        params: dict[str, Any] = {}
        if next_token:
            params["ContinuationToken"] = next_token
        # MaxBuckets is available on newer ListBuckets; keep optional via page_size.
        if self._page_size:
            params["MaxBuckets"] = min(self._page_size, 1000)
        response = self._invoke(client, "list_buckets", params, region=region)
        buckets = [
            b for b in (response.get("Buckets") or []) if isinstance(b, Mapping)
        ]
        items: list[NormalizedResource] = []
        for bucket in buckets:
            name = bucket.get("Name")
            if not name:
                continue
            # ListBuckets returns BucketRegion directly; only fall back to one
            # GetBucketLocation per bucket (per regional run) when it's absent.
            listed_region = bucket.get("BucketRegion")
            if isinstance(listed_region, str) and listed_region.strip():
                bucket_region: str | None = listed_region.strip()
            else:
                bucket_region = self._resolve_region(client, str(name), region=region)
            if bucket_region is None:
                continue
            if bucket_region != region:
                # Belong to another region — skip for this regional run.
                continue
            regional_client = self._client_for_bucket_region(bucket_region)
            items.append(
                self._collect_bucket_metadata(
                    regional_client,
                    bucket=bucket,
                    bucket_region=bucket_region,
                    account_id=account_id,
                )
            )
        token = response.get("ContinuationToken") or response.get("NextContinuationToken")
        return ResourcePage(items=items, next_token=str(token) if token else None)

    def normalize(
        self, raw: Mapping[str, Any], *, account_id: str, region: str
    ) -> NormalizedResource:
        # Unused — fetch_page builds resources via _collect_bucket_metadata.
        name = str(raw.get("Name") or "")
        return NormalizedResource(
            account_id=account_id,
            region=region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=name,
            observed_state=self._base_state(
                account_id=account_id,
                region=region,
                resource_id=name,
                name=name,
                discovery_timestamp=datetime.now(UTC).isoformat(),
            ),
        )

    def _client_for_bucket_region(self, bucket_region: str) -> Any:
        if self._client_factory is not None:
            return self._client_factory(bucket_region)
        return self._session_manager.client(
            self._service_name,
            region=bucket_region,
            require_configured_region=False,
        )

    def _resolve_region(
        self, client: Any, bucket_name: str, *, region: str
    ) -> str | None:
        response = self._invoke_soft(
            client,
            "get_bucket_location",
            {"Bucket": bucket_name},
            region=region,
            context=f"bucket={bucket_name}",
        )
        if response is None:
            return None
        return resolve_bucket_region(
            response.get("LocationConstraint"),
            default_region=self._s3_default_region,
            alias_map=self._location_map,
        )

    def _collect_bucket_metadata(
        self,
        client: Any,
        *,
        bucket: Mapping[str, Any],
        bucket_region: str,
        account_id: str,
    ) -> NormalizedResource:
        name = str(bucket["Name"])
        versioning = self._soft_config(
            client, "get_bucket_versioning", {"Bucket": name}, region=bucket_region
        )
        encryption = self._soft_config(
            client,
            "get_bucket_encryption",
            {"Bucket": name},
            region=bucket_region,
        )
        public_access = self._soft_config(
            client,
            "get_public_access_block",
            {"Bucket": name},
            region=bucket_region,
        )
        ownership = self._soft_config(
            client,
            "get_bucket_ownership_controls",
            {"Bucket": name},
            region=bucket_region,
        )

        encryption_rules = []
        if isinstance(encryption, Mapping):
            cfg = encryption.get("ServerSideEncryptionConfiguration") or {}
            for rule in cfg.get("Rules") or []:
                if not isinstance(rule, Mapping):
                    continue
                default = rule.get("ApplyServerSideEncryptionByDefault") or {}
                # Record algorithm / KMS key *id* only — never key material.
                encryption_rules.append(
                    {
                        "sse_algorithm": default.get("SSEAlgorithm"),
                        "kms_master_key_id": default.get("KMSMasterKeyID"),
                        "bucket_key_enabled": rule.get("BucketKeyEnabled"),
                    }
                )

        pab = {}
        if isinstance(public_access, Mapping):
            cfg = public_access.get("PublicAccessBlockConfiguration") or {}
            if isinstance(cfg, Mapping):
                pab = {
                    "block_public_acls": cfg.get("BlockPublicAcls"),
                    "ignore_public_acls": cfg.get("IgnorePublicAcls"),
                    "block_public_policy": cfg.get("BlockPublicPolicy"),
                    "restrict_public_buckets": cfg.get("RestrictPublicBuckets"),
                }

        ownership_rules = []
        if isinstance(ownership, Mapping):
            controls = ownership.get("OwnershipControls") or {}
            for rule in controls.get("Rules") or []:
                if isinstance(rule, Mapping):
                    ownership_rules.append(
                        {"object_ownership": rule.get("ObjectOwnership")}
                    )

        return NormalizedResource(
            account_id=account_id,
            region=bucket_region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=name,
            observed_state=self._base_state(
                account_id=account_id,
                region=bucket_region,
                resource_id=name,
                name=name,
                status="available",
                creation_date=_parse_dt(bucket.get("CreationDate")),
                versioning_status=(
                    versioning.get("Status") if isinstance(versioning, Mapping) else None
                ),
                mfa_delete=(
                    versioning.get("MFADelete")
                    if isinstance(versioning, Mapping)
                    else None
                ),
                encryption_rules=encryption_rules,
                public_access_block=pab,
                ownership_controls=ownership_rules,
                discovery_timestamp=datetime.now(UTC).isoformat(),
                # Explicit guardrail marker for audits.
                data_plane_access=False,
            ),
        )

    def _soft_config(
        self,
        client: Any,
        method: str,
        params: dict[str, Any],
        *,
        region: str,
    ) -> Mapping[str, Any] | None:
        return self._invoke_soft(
            client, method, params, region=region, context=method
        )


def create_s3_collectors(
    session_manager: AWSSessionManager,
    *,
    s3_service_name: str,
    s3_default_region: str,
    s3_location_constraint_map: Mapping[str, str] | None = None,
    page_size: int = 100,
    max_retries: int = 5,
) -> list[PaginatedCollector]:
    return [
        S3BucketCollector(
            session_manager,
            s3_service_name=s3_service_name,
            s3_default_region=s3_default_region,
            s3_location_constraint_map=s3_location_constraint_map,
            page_size=page_size,
            max_retries=max_retries,
        )
    ]
