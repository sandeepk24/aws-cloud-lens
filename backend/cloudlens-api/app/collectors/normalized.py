"""Normalized inventory payloads produced by collectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


def build_identity_key(*, arn: str | None, resource_id: str) -> str:
    """Prefer ARN when present; otherwise use the canonical resource ID."""
    if arn and arn.strip():
        return arn.strip()
    if not resource_id or not resource_id.strip():
        raise ValueError("resource_id is required when arn is absent")
    return resource_id.strip()


# ``to_resource_type`` value meaning "resolve the target by ARN, any type".
ANY_RESOURCE_TYPE = "*"

EdgeConfidence = Literal["verified", "inferred"]


@dataclass(slots=True)
class ResourceEdge:
    """A relationship from the current resource to another resource.

    ``confidence`` is ``"verified"`` when the target identifier was read directly
    from the AWS API response for the source resource, and ``"inferred"`` when it
    was derived (e.g. matched by IP address). ``evidence`` names the API field
    the identifier came from, e.g. ``"DescribeInstances:SubnetId"``.
    """

    relationship_type: str
    to_account_id: str
    to_region: str
    to_service: str
    to_resource_type: str
    to_resource_id: str
    to_arn: str | None = None
    confidence: EdgeConfidence = "verified"
    evidence: str | None = None

    @property
    def to_identity_key(self) -> str:
        return build_identity_key(arn=self.to_arn, resource_id=self.to_resource_id)


@dataclass(slots=True)
class NormalizedResource:
    """Canonical representation of a discovered AWS resource."""

    account_id: str
    region: str
    service: str
    resource_type: str
    resource_id: str
    arn: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    observed_state: dict[str, Any] = field(default_factory=dict)
    relationships: list[ResourceEdge] = field(default_factory=list)
    discovered_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def identity_key(self) -> str:
        return build_identity_key(arn=self.arn, resource_id=self.resource_id)


@dataclass(slots=True)
class ResourcePage:
    """One page of collector results."""

    items: list[NormalizedResource]
    next_token: str | None = None


@dataclass(slots=True)
class CollectorError(Exception):
    """Collector failure surfaced to orchestration (optionally retryable)."""

    message: str
    error_code: str | None = None
    retryable: bool = False
    region: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message
