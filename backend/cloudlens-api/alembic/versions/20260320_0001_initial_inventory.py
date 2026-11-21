"""Initial inventory schema.

Revision ID: 20260320_0001
Revises:
Create Date: 2026-03-20 00:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260320_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "aws_accounts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("aws_account_id", sa.String(length=12), nullable=False),
        sa.Column("project_alias", sa.String(length=128), nullable=False),
        sa.Column("account_alias", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint("aws_account_id", name="uq_aws_accounts_aws_account_id"),
        sa.UniqueConstraint(
            "project_alias",
            "account_alias",
            name="uq_aws_accounts_project_account_alias",
        ),
    )
    op.create_index(
        "ix_aws_accounts_aws_account_id", "aws_accounts", ["aws_account_id"]
    )

    op.create_table(
        "inventory_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("account_pk", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("collector_name", sa.String(length=128), nullable=False),
        sa.Column("collector_version", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=True),
        sa.Column("service", sa.String(length=64), nullable=True),
        sa.Column("resource_type", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("resources_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "resources_upserted", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("resources_stale", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stats", sa.JSON(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_pk"], ["aws_accounts.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_inventory_runs_status", "inventory_runs", ["status"])
    op.create_index(
        "ix_inventory_runs_collector_name", "inventory_runs", ["collector_name"]
    )
    op.create_index("ix_inventory_runs_region", "inventory_runs", ["region"])

    op.create_table(
        "resources",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("account_pk", sa.String(length=36), nullable=False),
        sa.Column("aws_account_id", sa.String(length=12), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=False),
        sa.Column("service", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=128), nullable=False),
        sa.Column("resource_id", sa.String(length=512), nullable=False),
        sa.Column("arn", sa.String(length=2048), nullable=True),
        sa.Column("identity_key", sa.String(length=2048), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("observed_state", sa.JSON(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collector_version", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("is_stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inventory_run_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_pk"], ["aws_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["last_inventory_run_id"], ["inventory_runs.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "aws_account_id",
            "region",
            "service",
            "resource_type",
            "identity_key",
            name="uq_resources_natural_key",
        ),
    )
    op.create_index("ix_resources_aws_account_id", "resources", ["aws_account_id"])
    op.create_index("ix_resources_last_seen_at", "resources", ["last_seen_at"])
    op.create_index("ix_resources_is_stale", "resources", ["is_stale"])
    op.create_index(
        "ix_resources_account_service_type",
        "resources",
        ["aws_account_id", "service", "resource_type"],
    )

    op.create_table(
        "resource_relationships",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("from_resource_id", sa.String(length=36), nullable=False),
        sa.Column("to_resource_id", sa.String(length=36), nullable=False),
        sa.Column("relationship_type", sa.String(length=128), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collector_version", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["from_resource_id"], ["resources.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["to_resource_id"], ["resources.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "from_resource_id",
            "to_resource_id",
            "relationship_type",
            name="uq_resource_relationships_edge",
        ),
    )
    op.create_index(
        "ix_resource_relationships_relationship_type",
        "resource_relationships",
        ["relationship_type"],
    )

    op.create_table(
        "collection_errors",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("inventory_run_id", sa.String(length=36), nullable=False),
        sa.Column("account_pk", sa.String(length=36), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=True),
        sa.Column("service", sa.String(length=64), nullable=True),
        sa.Column("resource_type", sa.String(length=128), nullable=True),
        sa.Column("collector_name", sa.String(length=128), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["inventory_run_id"], ["inventory_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["account_pk"], ["aws_accounts.id"], ondelete="CASCADE"
        ),
    )


def downgrade() -> None:
    op.drop_table("collection_errors")
    op.drop_table("resource_relationships")
    op.drop_table("resources")
    op.drop_table("inventory_runs")
    op.drop_table("aws_accounts")
