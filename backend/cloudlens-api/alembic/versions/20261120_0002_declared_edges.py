"""Add resources.declared_edges for architecture graph evidence.

Revision ID: 20261120_0002
Revises: 20260320_0001
Create Date: 2026-11-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261120_0002"
down_revision = "20260320_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("resources") as batch:
        batch.add_column(
            sa.Column("declared_edges", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("resources") as batch:
        batch.drop_column("declared_edges")
