"""Optional, read-only Kubernetes workload discovery for EKS clusters.

The AWS EKS control-plane APIs (DescribeCluster etc.) do not expose workloads,
so Ingress -> Service -> Pod -> Node relationships can only come from the
Kubernetes API itself. This adapter reads them and nothing else.

Relationships emitted (all read from object fields, never inferred):

* ``k8s-ingress  -routes_to->   k8s-service``  Ingress backend service name
* ``k8s-endpointslice -belongs_to-> k8s-service``  label ``kubernetes.io/service-name``
  (set by the EndpointSlice controller)
* ``k8s-endpointslice -targets-> k8s-pod``  ``endpoints[].targetRef`` (kind Pod)
* ``k8s-pod -scheduled_on-> instance``  ``Pod.spec.nodeName`` + ``Node.spec.providerID``
  (``aws:///<az>/i-...``; Fargate nodes have no EC2 instance and get no edge)

Service -> Pod is NOT derived from label selectors: only EndpointSlice
``targetRef`` records (what the control plane actually selected) are used.
Ingress load-balancer hostnames are recorded in ``load_balancer_hostnames``;
matching them to an ALB is left to the graph builder (exact DNS match).

Required RBAC (read-only; bind via an EKS access entry or aws-auth group)::

    apiVersion: rbac.authorization.k8s.io/v1
    kind: ClusterRole
    metadata: {name: cloudlens-read}
    rules:
      - apiGroups: [""]
        resources: [services, pods, nodes]
        verbs: [get, list]
      - apiGroups: [networking.k8s.io]
        resources: [ingresses]
        verbs: [get, list]
      - apiGroups: [discovery.k8s.io]
        resources: [endpointslices]
        verbs: [get, list]

No create/update/patch/delete/watch/exec verbs are needed or used, and Secrets
and ConfigMaps are never read. Pod environment variables are not recorded.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from app.collectors.base import PaginatedCollector
from app.collectors.normalized import NormalizedResource, ResourceEdge, ResourcePage

K8sKind = Literal["ingress", "service", "pod", "endpointslice"]

RESOURCE_TYPES: dict[str, str] = {
    "ingress": "k8s-ingress",
    "service": "k8s-service",
    "pod": "k8s-pod",
    "endpointslice": "k8s-endpointslice",
}

KUBERNETES_SERVICE = "kubernetes"
_SERVICE_NAME_LABEL = "kubernetes.io/service-name"


class KubernetesAdapterUnavailableError(RuntimeError):
    """The optional ``kubernetes`` package is missing or the cluster is unreachable."""


class KubernetesReadClient(Protocol):
    """Read-only listing of cluster objects as plain dicts (Kubernetes JSON shape)."""

    def list_ingresses(self) -> list[dict[str, Any]]: ...

    def list_services(self) -> list[dict[str, Any]]: ...

    def list_pods(self) -> list[dict[str, Any]]: ...

    def list_endpoint_slices(self) -> list[dict[str, Any]]: ...

    def list_nodes(self) -> list[dict[str, Any]]: ...


def instance_id_from_provider_id(provider_id: Any) -> str | None:
    """``aws:///us-east-1a/i-0abc`` -> ``i-0abc``; anything else -> None."""
    if not isinstance(provider_id, str) or not provider_id.startswith("aws://"):
        return None
    last = provider_id.rstrip("/").rsplit("/", 1)[-1]
    return last if last.startswith("i-") else None


def _meta(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    meta = obj.get("metadata") or {}
    return meta if isinstance(meta, Mapping) else {}


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(key)
    return obj


class _Snapshot:
    """One read of the cluster shared by the per-kind collectors of a run."""

    def __init__(self, client: KubernetesReadClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._data: dict[str, list[dict[str, Any]]] | None = None

    def get(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            if self._data is None:
                self._data = {
                    "ingress": list(self._client.list_ingresses()),
                    "service": list(self._client.list_services()),
                    "pod": list(self._client.list_pods()),
                    "endpointslice": list(self._client.list_endpoint_slices()),
                    "node": list(self._client.list_nodes()),
                }
            return self._data


class KubernetesWorkloadCollector(PaginatedCollector):
    """Collect one Kubernetes object kind for one EKS cluster.

    The orchestrator keeps only items whose resource_type matches the collector,
    so each kind is its own collector; use :func:`create_kubernetes_collectors`
    to build all four over a single shared cluster snapshot.
    """

    service = KUBERNETES_SERVICE
    version = "0.1.0"
    source = "kubernetes"

    def __init__(
        self,
        read_client: KubernetesReadClient,
        *,
        cluster_name: str,
        cluster_region: str,
        kind: K8sKind,
        ec2_service_name: str = "ec2",
        snapshot: _Snapshot | None = None,
    ) -> None:
        if kind not in RESOURCE_TYPES:
            raise ValueError(f"unsupported kubernetes kind: {kind}")
        self.kind = kind
        self.name = f"kubernetes.{kind}"
        self.resource_type = RESOURCE_TYPES[kind]
        self._cluster = cluster_name.strip()
        self._region = cluster_region.strip()
        self._ec2_service_name = ec2_service_name.strip()
        self._snapshot = snapshot or _Snapshot(read_client)

    def fetch_page(
        self,
        *,
        account_id: str,
        region: str,
        next_token: str | None = None,
    ) -> ResourcePage:
        if region != self._region:
            return ResourcePage(items=[])
        data = self._snapshot.get()
        builder = {
            "ingress": self._ingress,
            "service": self._service,
            "pod": self._pod,
            "endpointslice": self._endpoint_slice,
        }[self.kind]
        nodes = {
            str(_meta(n).get("name")): _get(n, "spec", "providerID")
            for n in data["node"]
            if _meta(n).get("name")
        }
        items = [
            builder(obj, account_id=account_id, nodes=nodes)
            for obj in data[self.kind]
            if _meta(obj).get("name")
        ]
        return ResourcePage(items=items)

    # -- helpers ---------------------------------------------------------------
    def _rid(self, namespace: Any, name: Any) -> str:
        return f"{self._cluster}/{namespace or 'default'}/{name}"

    def _edge(
        self,
        rel: str,
        rtype: str,
        rid: str,
        account_id: str,
        evidence: str,
        service: str = KUBERNETES_SERVICE,
    ) -> ResourceEdge:
        return ResourceEdge(
            relationship_type=rel,
            to_account_id=account_id,
            to_region=self._region,
            to_service=service,
            to_resource_type=rtype,
            to_resource_id=rid,
            evidence=evidence,
        )

    def _resource(
        self,
        obj: Mapping[str, Any],
        *,
        account_id: str,
        state: dict[str, Any],
        relationships: list[ResourceEdge],
    ) -> NormalizedResource:
        meta = _meta(obj)
        rid = self._rid(meta.get("namespace"), meta.get("name"))
        labels = meta.get("labels") or {}
        return NormalizedResource(
            account_id=account_id,
            region=self._region,
            service=self.service,
            resource_type=self.resource_type,
            resource_id=rid,
            tags={str(k): str(v) for k, v in labels.items()} if isinstance(labels, Mapping) else {},
            observed_state={
                "account_id": account_id,
                "region": self._region,
                "resource_id": rid,
                "name": meta.get("name"),
                "namespace": meta.get("namespace") or "default",
                "cluster_name": self._cluster,
                "uid": meta.get("uid"),
                **state,
                "discovery_timestamp": datetime.now(UTC).isoformat(),
            },
            relationships=relationships,
        )

    def _ingress(
        self, obj: Mapping[str, Any], *, account_id: str, nodes: Any
    ) -> NormalizedResource:
        ns = _meta(obj).get("namespace") or "default"
        rules: list[dict[str, Any]] = []
        backends: dict[str, str] = {}
        default_svc = _get(obj, "spec", "defaultBackend", "service", "name")
        if default_svc:
            backends[str(default_svc)] = "Ingress.spec.defaultBackend.service.name"
        for rule in _get(obj, "spec", "rules") or []:
            for path in _get(rule, "http", "paths") or []:
                svc = _get(path, "backend", "service", "name")
                port = _get(path, "backend", "service", "port")
                rules.append(
                    {
                        "host": _get(rule, "host"),
                        "path": _get(path, "path"),
                        "service": svc,
                        "port": (port.get("number") or port.get("name"))
                        if isinstance(port, Mapping)
                        else None,
                    }
                )
                if svc:
                    backends.setdefault(
                        str(svc), "Ingress.spec.rules[].http.paths[].backend.service.name"
                    )
        hostnames = [
            str(i.get("hostname")).lower().rstrip(".")
            for i in _get(obj, "status", "loadBalancer", "ingress") or []
            if isinstance(i, Mapping) and i.get("hostname")
        ]
        rels = [
            self._edge("routes_to", "k8s-service", self._rid(ns, svc), account_id, ev)
            for svc, ev in backends.items()
        ]
        state = {
            "status": "available",
            "ingress_class": _get(obj, "spec", "ingressClassName"),
            "rules": rules,
            "load_balancer_hostnames": hostnames,
        }
        return self._resource(obj, account_id=account_id, state=state, relationships=rels)

    def _service(
        self, obj: Mapping[str, Any], *, account_id: str, nodes: Any
    ) -> NormalizedResource:
        hostnames = [
            str(i.get("hostname")).lower().rstrip(".")
            for i in _get(obj, "status", "loadBalancer", "ingress") or []
            if isinstance(i, Mapping) and i.get("hostname")
        ]
        state = {
            "status": "available",
            "service_type": _get(obj, "spec", "type"),
            "cluster_ip": _get(obj, "spec", "clusterIP"),
            "ports": [
                {
                    "port": p.get("port"),
                    "target_port": p.get("targetPort"),
                    "protocol": p.get("protocol"),
                }
                for p in _get(obj, "spec", "ports") or []
                if isinstance(p, Mapping)
            ],
            # Recorded for reference only — never used to link pods.
            "selector": dict(_get(obj, "spec", "selector") or {}),
            "load_balancer_hostnames": hostnames,
        }
        return self._resource(obj, account_id=account_id, state=state, relationships=[])

    def _endpoint_slice(
        self, obj: Mapping[str, Any], *, account_id: str, nodes: Any
    ) -> NormalizedResource:
        meta = _meta(obj)
        ns = meta.get("namespace") or "default"
        labels = meta.get("labels") or {}
        owner = labels.get(_SERVICE_NAME_LABEL) if isinstance(labels, Mapping) else None
        rels: list[ResourceEdge] = []
        if owner:
            rels.append(
                self._edge(
                    "belongs_to",
                    "k8s-service",
                    self._rid(ns, owner),
                    account_id,
                    f"EndpointSlice.metadata.labels[{_SERVICE_NAME_LABEL}]",
                )
            )
        addresses: list[str] = []
        pods: list[str] = []
        for endpoint in obj.get("endpoints") or []:
            if not isinstance(endpoint, Mapping):
                continue
            addresses.extend(str(a) for a in endpoint.get("addresses") or [])
            ref = endpoint.get("targetRef") or {}
            if isinstance(ref, Mapping) and ref.get("kind") == "Pod" and ref.get("name"):
                pod_rid = self._rid(ref.get("namespace") or ns, ref["name"])
                if pod_rid not in pods:
                    pods.append(pod_rid)
                    rels.append(
                        self._edge(
                            "targets",
                            "k8s-pod",
                            pod_rid,
                            account_id,
                            "EndpointSlice.endpoints[].targetRef",
                        )
                    )
        state = {
            "status": "available",
            "service_name": owner,
            "address_type": obj.get("addressType"),
            "addresses": addresses,
            "target_pods": pods,
        }
        return self._resource(obj, account_id=account_id, state=state, relationships=rels)

    def _pod(
        self, obj: Mapping[str, Any], *, account_id: str, nodes: dict[str, Any]
    ) -> NormalizedResource:
        node_name = _get(obj, "spec", "nodeName")
        provider_id = nodes.get(str(node_name)) if node_name else None
        instance_id = instance_id_from_provider_id(provider_id)
        rels: list[ResourceEdge] = []
        if instance_id:
            rels.append(
                self._edge(
                    "scheduled_on",
                    "instance",
                    instance_id,
                    account_id,
                    "Pod.spec.nodeName+Node.spec.providerID",
                    service=self._ec2_service_name,
                )
            )
        owners = [
            {"kind": o.get("kind"), "name": o.get("name")}
            for o in _meta(obj).get("ownerReferences") or []
            if isinstance(o, Mapping)
        ]
        state = {
            "status": _get(obj, "status", "phase"),
            "phase": _get(obj, "status", "phase"),
            "pod_ip": _get(obj, "status", "podIP"),
            "host_ip": _get(obj, "status", "hostIP"),
            "node_name": node_name,
            "node_provider_id": provider_id,
            "instance_id": instance_id,
            "owner_references": owners,
            "container_images": [
                c.get("image")
                for c in _get(obj, "spec", "containers") or []
                if isinstance(c, Mapping)
            ],
        }
        return self._resource(obj, account_id=account_id, state=state, relationships=rels)


def create_kubernetes_collectors(
    read_client: KubernetesReadClient,
    *,
    cluster_name: str,
    cluster_region: str,
    ec2_service_name: str = "ec2",
) -> list[KubernetesWorkloadCollector]:
    """All four kinds over one shared snapshot (the cluster is listed once)."""
    snapshot = _Snapshot(read_client)
    return [
        KubernetesWorkloadCollector(
            read_client,
            cluster_name=cluster_name,
            cluster_region=cluster_region,
            kind=kind,
            ec2_service_name=ec2_service_name,
            snapshot=snapshot,
        )
        for kind in ("ingress", "service", "endpointslice", "pod")
    ]


class _KubeconfigClient:
    """Real client over the optional ``kubernetes`` package (list calls only)."""

    def __init__(self, api_client: Any, core: Any, networking: Any, discovery: Any) -> None:
        self._api_client = api_client
        self._core = core
        self._networking = networking
        self._discovery = discovery

    def _list(self, fn: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"limit": 500}
            if token:
                kwargs["_continue"] = token
            result = fn(**kwargs)
            out.extend(self._api_client.sanitize_for_serialization(i) for i in result.items)
            token = getattr(result.metadata, "_continue", None)
            if not token:
                return out

    def list_ingresses(self) -> list[dict[str, Any]]:
        return self._list(self._networking.list_ingress_for_all_namespaces)

    def list_services(self) -> list[dict[str, Any]]:
        return self._list(self._core.list_service_for_all_namespaces)

    def list_pods(self) -> list[dict[str, Any]]:
        return self._list(self._core.list_pod_for_all_namespaces)

    def list_endpoint_slices(self) -> list[dict[str, Any]]:
        return self._list(self._discovery.list_endpoint_slice_for_all_namespaces)

    def list_nodes(self) -> list[dict[str, Any]]:
        return self._list(self._core.list_node)


def kubeconfig_read_client(
    context: str, kubeconfig_path: str | None = None
) -> KubernetesReadClient:
    """Build a read client from a kubeconfig context (requires ``kubernetes``)."""
    try:
        from kubernetes import client as k8s_client
        from kubernetes import config as k8s_config
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise KubernetesAdapterUnavailableError(
            "Kubernetes discovery needs the optional 'kubernetes' package "
            "(pip install kubernetes); skipping workload discovery."
        ) from exc
    try:
        api_client = k8s_config.new_client_from_config(config_file=kubeconfig_path, context=context)
    except Exception as exc:  # noqa: BLE001 - surface as adapter unavailability
        raise KubernetesAdapterUnavailableError(
            f"Unable to load kubeconfig context '{context}': {exc}"
        ) from exc
    return _KubeconfigClient(
        api_client,
        k8s_client.CoreV1Api(api_client),
        k8s_client.NetworkingV1Api(api_client),
        k8s_client.DiscoveryV1Api(api_client),
    )
