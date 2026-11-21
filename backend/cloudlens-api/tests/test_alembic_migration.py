"""Smoke-test that the initial Alembic migration upgrades cleanly."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


@pytest.fixture
def alembic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    db_path = tmp_path / "migrate.db"
    url = f"sqlite+pysqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)

    # Clear cached settings so DATABASE_URL is picked up.
    from app.core.config import get_settings

    get_settings.cache_clear()

    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_alembic_upgrade_head(alembic_config: Config, tmp_path: Path) -> None:
    command.upgrade(alembic_config, "head")
    db_path = tmp_path / "migrate.db"
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert {
        "aws_accounts",
        "inventory_runs",
        "resources",
        "resource_relationships",
        "collection_errors",
        "alembic_version",
    } <= tables
    engine.dispose()

    from app.core.config import get_settings

    get_settings.cache_clear()
