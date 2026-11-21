"""Tests for the optional read-only Kubernetes workload adapter."""

from __future__ import annotations

from typing import Any

import pytest

from app.collectors.kubernetes import (
    KubernetesWorkloadCollector,
    create_kubernetes_collectors,
    instance_id_from_provider_id,
)

ACCOUNT = "111122223333"
REGION = "test-region-a"


class FakeCluster:
    """Fake read client; counts calls and exposes no write methods at all."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _record(self, name: str, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.calls.append(name)
        return value

    def list_ingresses(self) -> list[dict[str, Any]]:
        return self._record(
            "ingresses",
            [
                {
                    "metadata": {"name": "web", "namespace": "shop"},
                    "spec": {
                        "ingressClassName": "alb",
                        "rules": [
                            {
                                "host": "shop.example.com",
                                "http": {
                                    "paths": [
                                        {
                                            "path": "/",
                                            "backend": {
                                                "service": {
                                                    "name": "frontend",
                                                    "port": {"number": 80},
                                                }
                                            },
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                    "status": {
                        "loadBalancer": {
                            "ingress": [{"hostname": "k8s-shop-web-1.ELB.amazonaws.com."}]
                        }
                    },
                }
            ],
        )

    def list_services(self) -> list[dict[str, Any]]:
        return self._record(
            "services",
            [
                {
                    "metadata": {"name": "frontend", "namespace": "shop"},
                    "spec": {
                        "type": "ClusterIP",
                        "selector": {"app": "frontend"},
                        "ports": [{"port": 80, "targetPort": 8080}],
                    },
                },
                # Same selector as frontend but no EndpointSlice: must not get pod links.
                {
                    "metadata": {"name": "shadow", "namespace": "shop"},
                    "spec": {"selector": {"app": "frontend"}},
                },
            ],
        )

    def list_pods(self) -> list[dict[str, Any]]:
        return self._record(
            "pods",
            [
                {
                    "metadata": {
                        "name": "frontend-1",
                        "namespace": "shop",
                        "labels": {"app": "frontend"},
                    },
                    "spec": {"nodeName": "ip-10-0-1-5", "containers": [{"image": "shop:1"}]},
                    "status": {"phase": "Running", "podIP": "10.0.1.20"},
                },
                {
                    "metadata": {
                        "name": "frontend-2",
                        "namespace": "shop",
                        "labels": {"app": "frontend"},
                    },
                    "spec": {"nodeName": "fargate-ip-10-0-2-9"},
                    "status": {"phase": "Running"},
                },
            ],
        )

    def list_endpoint_slices(self) -> list[dict[str, Any]]:
        return self._record(
            "endpointslices",
            [
                {
                    "metadata": {
                        "name": "frontend-abc",
                        "namespace": "shop",
                        "labels": {"kubernetes.io/service-name": "frontend"},
                    },
                    "addressType": "IPv4",
                    "endpoints": [
                        {
                            "addresses": ["10.0.1.20"],
                            "targetRef": {"kind": "Pod", "name": "frontend-1", "namespace": "shop"},
                        },
                        {"addresses": ["10.0.9.9"]},  # no targetRef -> no pod edge
                    ],
                }
            ],
        )

    def list_nodes(self) -> list[dict[str, Any]]:
        return self._record(
            "nodes",
            [
                {
                    "metadata": {"name": "ip-10-0-1-5"},
                    "spec": {"providerID": "aws:///test-region-aa/i-0abc123"},
                },
                {
                    "metadata": {"name": "fargate-ip-10-0-2-9"},
                    "spec": {"providerID": "aws:///test-region-ab/abc123/fargate-ip-10-0-2-9"},
                },
            ],
        )


def _collect(cluster: FakeCluster) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for collector in create_kubernetes_collectors(
        cluster, cluster_name="prod", cluster_region=REGION, ec2_service_name="ec2"
    ):
        out[collector.resource_type] = collector.fetch_page(account_id=ACCOUNT, region=REGION).items
    return out


def _edges(item: Any) -> set[tuple[str, str, str]]:
    return {(e.relationship_type, e.to_resource_type, e.to_resource_id) for e in item.relationships}


def test_provider_id_parsing() -> None:
    assert instance_id_from_provider_id("aws:///us-east-1a/i-0abc") == "i-0abc"
    assert instance_id_from_provider_id("aws:///us-east-1a/x/fargate-ip-1") is None
    assert instance_id_from_provider_id("gce://p/z/n") is None
    assert instance_id_from_provider_id(None) is None


def test_workload_relationships_come_only_from_object_fields() -> None:
    cluster = FakeCluster()
    items = _collect(cluster)

    ingress = items["k8s-ingress"][0]
    assert ingress.resource_id == "prod/shop/web"
    assert _edges(ingress) == {("routes_to", "k8s-service", "prod/shop/frontend")}
    assert ingress.observed_state["load_balancer_hostnames"] == ["k8s-shop-web-1.elb.amazonaws.com"]

    services = {s.resource_id: s for s in items["k8s-service"]}
    # Selector matches pods, but services never link to pods directly.
    assert all(s.relationships == [] for s in services.values())

    eps = items["k8s-endpointslice"][0]
    assert _edges(eps) == {
        ("belongs_to", "k8s-service", "prod/shop/frontend"),
        ("targets", "k8s-pod", "prod/shop/frontend-1"),
    }

    pods = {p.resource_id: p for p in items["k8s-pod"]}
    assert _edges(pods["prod/shop/frontend-1"]) == {("scheduled_on", "instance", "i-0abc123")}
    ec2_edge = pods["prod/shop/frontend-1"].relationships[0]
    assert ec2_edge.to_service == "ec2" and ec2_edge.to_region == REGION
    assert pods["prod/shop/frontend-2"].relationships == []  # Fargate node: no instance

    for group in items.values():
        for item in group:
            assert item.service == "kubernetes" and item.region == REGION
            assert all(e.evidence and e.confidence == "verified" for e in item.relationships)

    # One shared snapshot: every kind listed exactly once.
    assert sorted(cluster.calls) == sorted(
        ["ingresses", "services", "pods", "endpointslices", "nodes"]
    )


def test_other_regions_are_skipped_without_cluster_calls() -> None:
    cluster = FakeCluster()
    collector = KubernetesWorkloadCollector(
        cluster, cluster_name="prod", cluster_region=REGION, kind="pod"
    )
    assert collector.fetch_page(account_id=ACCOUNT, region="other-region").items == []
    assert cluster.calls == []


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValueError):
        KubernetesWorkloadCollector(
            FakeCluster(), cluster_name="c", cluster_region=REGION, kind="secret"
        )  # type: ignore[arg-type]


def test_kubeconfig_client_requires_optional_package() -> None:
    try:
        import kubernetes  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        from app.collectors.kubernetes import (
            KubernetesAdapterUnavailableError,
            kubeconfig_read_client,
        )

        with pytest.raises(KubernetesAdapterUnavailableError, match="optional 'kubernetes'"):
            kubeconfig_read_client("ctx")
    else:  # pragma: no cover
        pytest.skip("kubernetes package installed; unavailability path not testable")
