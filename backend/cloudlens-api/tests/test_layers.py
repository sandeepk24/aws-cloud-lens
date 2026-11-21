"""Unit tests for connector, collector, and analytics layers."""

from pathlib import Path

from app.accounts.loader import load_accounts_config
from app.analytics.engine import AnalyticsEngine
from app.collectors.base import BaseCollector, CollectionResult, CollectorRegistry
from app.connectors.aws.client import connector_from_account

VALID_YAML = Path(__file__).parent / "fixtures" / "accounts.valid.yaml"


def test_aws_connector_describe_has_no_live_calls() -> None:
    config = load_accounts_config(VALID_YAML)
    project, account = config.find_account("demo-project", "profile-account")
    connector = connector_from_account(project, account)
    meta = connector.describe()
    assert meta["provider"] == "aws"
    assert meta["live_calls_enabled"] is False
    assert meta["regions"] == ["test-region-a"]
    assert meta["auth_type"] == "profile"
    assert connector.build_session_kwargs() == {
        "region_name": "test-region-a",
        "profile_name": "test-profile",
    }


class _StubCollector(BaseCollector):
    name = "stub-ec2"
    resource_type = "ec2.instance"

    def collect(self) -> CollectionResult:
        return CollectionResult(
            collector=self.name,
            resource_type=self.resource_type,
            items=[{"id": "i-1"}, {"id": "i-2"}],
        )


def test_collector_registry_and_analytics() -> None:
    registry = CollectorRegistry()
    registry.register(_StubCollector())
    assert registry.list() == ["stub-ec2"]

    results = registry.run_all()
    summary = AnalyticsEngine().summarize(results)
    assert summary.total_resources == 2
    assert summary.by_resource_type["ec2.instance"] == 2
    assert summary.collectors_run == ["stub-ec2"]


def test_analytics_empty_results() -> None:
    summary = AnalyticsEngine().summarize([])
    assert summary.total_resources == 0
    assert summary.notes
