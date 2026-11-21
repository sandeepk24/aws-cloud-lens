"""Dialect-aware upsert helpers for inventory persistence."""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session


def upsert_mappings(
    session: Session,
    *,
    table: Any,
    rows: list[dict[str, Any]],
    conflict_columns: list[str],
    update_columns: list[str],
) -> int:
    """Insert or update ``rows`` on ``conflict_columns``.

    Returns the number of rows submitted (dialects may not report exact upserts).
    """
    if not rows:
        return 0

    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else "sqlite"

    if dialect == "postgresql":
        stmt = pg_insert(table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=conflict_columns,
            set_={col: stmt.excluded[col] for col in update_columns},
        )
    else:
        # SQLite (tests / local) and fallback
        stmt = sqlite_insert(table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=conflict_columns,
            set_={col: stmt.excluded[col] for col in update_columns},
        )

    session.execute(stmt)
    return len(rows)


def upsert_connection(
    connection: Connection,
    *,
    table: Any,
    rows: list[dict[str, Any]],
    conflict_columns: list[str],
    update_columns: list[str],
) -> int:
    if not rows:
        return 0
    dialect = connection.dialect.name
    if dialect == "postgresql":
        stmt = pg_insert(table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=conflict_columns,
            set_={col: stmt.excluded[col] for col in update_columns},
        )
    else:
        stmt = sqlite_insert(table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=conflict_columns,
            set_={col: stmt.excluded[col] for col in update_columns},
        )
    connection.execute(stmt)
    return len(rows)
