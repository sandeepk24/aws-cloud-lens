"""Shared inventory enumerations."""

from __future__ import annotations

import enum


class InventoryRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class RelationshipDirection(str, enum.Enum):
    """Logical direction of a stored edge (from → to)."""

    FORWARD = "forward"
