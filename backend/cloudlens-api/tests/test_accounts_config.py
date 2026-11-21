"""Tests for external YAML accounts configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from app.accounts.loader import (
    AccountsConfigError,
    clear_accounts_config_cache,
    get_accounts_config,
    load_accounts_config,
)
from app.accounts.models import AssumeRoleAuth, ProfileAuth
from app.connectors.aws.client import (
    connector_from_account,
    create_aws_connectors,
    get_aws_connector,
)
from app.core.config import get_settings

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[3]
VALID_YAML = FIXTURES / "accounts.valid.yaml"
INVALID_YAML = FIXTURES / "accounts.invalid.yaml"
EXAMPLE_YAML = REPO_ROOT / "config" / "accounts.example.yaml"
SCHEMA_JSON = REPO_ROOT / "config" / "schemas" / "accounts.schema.json"


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_accounts_config_cache()
    get_settings.cache_clear()
    yield
    clear_accounts_config_cache()
    get_settings.cache_clear()


def test_load_valid_accounts_config() -> None:
    config = load_accounts_config(VALID_YAML)
    assert config.version == "1"
    assert len(config.projects) == 1
    project, profile_account = config.find_account("demo-project", "profile-account")
    assert project.alias == "demo-project"
    assert isinstance(profile_account.auth, ProfileAuth)
    assert profile_account.regions == ["test-region-a"]
    assert profile_account.collectors.enabled == ["inventory.compute"]

    _, role_account = config.find_account("demo-project", "role-account")
    assert isinstance(role_account.auth, AssumeRoleAuth)
    assert role_account.auth.role_arn.endswith(":role/TestReadOnly")


def test_load_invalid_accounts_config_raises() -> None:
    with pytest.raises(AccountsConfigError):
        load_accounts_config(INVALID_YAML)


def test_missing_cloudlens_config_path_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLOUDLENS_CONFIG_PATH", "")
    get_settings.cache_clear()
    clear_accounts_config_cache()
    with pytest.raises(AccountsConfigError, match="CLOUDLENS_CONFIG_PATH"):
        get_accounts_config()


def test_cloudlens_config_path_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDLENS_CONFIG_PATH", str(VALID_YAML))
    get_settings.cache_clear()
    clear_accounts_config_cache()
    config = get_accounts_config()
    assert config.projects[0].alias == "demo-project"


def test_connectors_built_from_config() -> None:
    connectors = []
    # Avoid process cache: load explicitly then build via public helpers.
    monkeypatch_path = VALID_YAML
    config = load_accounts_config(monkeypatch_path)
    for project, account in config.iter_accounts():
        connectors.append(connector_from_account(project, account))

    assert len(connectors) == 2
    profile_connector = connectors[0]
    assert profile_connector.describe()["auth_type"] == "profile"
    assert profile_connector.build_session_kwargs() == {
        "region_name": "test-region-a",
        "profile_name": "test-profile",
    }

    role_connector = connectors[1]
    assert role_connector.describe()["auth_type"] == "assume_role"
    assert role_connector.sts_region() == "test-region-b"
    assume_kwargs = role_connector.build_assume_role_kwargs()
    assert assume_kwargs["RoleArn"] == "arn:aws:iam::444455556666:role/TestReadOnly"
    assert assume_kwargs["ExternalId"] == "test-external-id"
    assert assume_kwargs["RoleSessionName"] == "cloudlens-test"
    assert assume_kwargs["DurationSeconds"] == 3600
    assert role_connector.build_session_kwargs("test-region-c") == {
        "region_name": "test-region-c",
        "profile_name": "test-source-profile",
    }


def test_get_aws_connector_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDLENS_CONFIG_PATH", str(VALID_YAML))
    get_settings.cache_clear()
    clear_accounts_config_cache()
    connector = get_aws_connector("demo-project", "role-account")
    assert connector.account_alias == "role-account"
    assert create_aws_connectors()[0].account_alias == "profile-account"


def test_reject_unconfigured_region() -> None:
    config = load_accounts_config(VALID_YAML)
    project, account = config.find_account("demo-project", "profile-account")
    connector = connector_from_account(project, account)
    with pytest.raises(ValueError, match="not configured"):
        connector.build_session_kwargs("not-in-config")


def test_example_yaml_matches_json_schema() -> None:
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    document = yaml.safe_load(EXAMPLE_YAML.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)


def test_valid_fixture_matches_json_schema() -> None:
    schema = json.loads(SCHEMA_JSON.read_text(encoding="utf-8"))
    document = yaml.safe_load(VALID_YAML.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(document)


def test_example_yaml_validates_with_pydantic() -> None:
    # Placeholders are intentional; structure must still validate.
    config = load_accounts_config(EXAMPLE_YAML)
    assert config.projects[0].alias == "example-project"
