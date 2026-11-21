"""Graph builder, views and exports over persisted inventory (no AWS calls)."""

from __future__ import annotations

import os
import stat
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.accounts.models import AccountsConfig
from app.api.routes.graph import _config as config_dep
from app.collectors.normalized import ANY_RESOURCE_TYPE, NormalizedResource, ResourceEdge
from app.db.base import Base
from app.db.repository import create_inventory_run, get_or_create_account, upsert_resources
from app.db.session import get_db
from app.graph.artifacts import write_artifacts
from app.graph.export import to_json, to_mermaid, to_svg
from app.graph.models import GraphDocument, GraphScope
from app.graph.service import GraphScopeError, build_document, iam_action_for, node_detail
from app.main import create_app

A, B = "111122223333", "444455556666"
R = "us-east-1"
LB_ARN = f"arn:aws:elasticloadbalancing:{R}:{A}:loadbalancer/app/web/1"
TG_ARN = f"arn:aws:elasticloadbalancing:{R}:{A}:targetgroup/web/2"
L_ARN = f"arn:aws:elasticloadbalancing:{R}:{A}:listener/app/web/1/3"


def _config() -> AccountsConfig:
    acct = lambda alias, aid: {  # noqa: E731
        "alias": alias,
        "account_id": aid,
        "regions": [R],
        "collectors": {"enabled": []},
        "auth": {"type": "profile", "profile": "x"},
    }
    return AccountsConfig.model_validate(
        {
            "version": "1",
            "aws_api": {
                **{
                    f"{s}_service_name": s
                    for s in [
                        "sts",
                        "ec2",
                        "elbv2",
                        "ecs",
                        "eks",
                        "rds",
                        "s3",
                        "wafv2",
                        "route53",
                        "logs",
                    ]
                },
                "global_control_region": R,
                "s3_default_region": R,
            },
            "projects": [
                {"alias": "shop", "accounts": [acct("prod", A)]},
                {"alias": "other", "accounts": [acct("other", B)]},
            ],
        }
    )


@pytest.fixture
def db(tmp_path) -> Iterator[Session]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'g.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


def _edge(
    rel: str,
    rtype: str,
    rid: str,
    *,
    account: str = A,
    service: str = "ec2",
    confidence: str = "verified",
    evidence: str = "Test:Field",
) -> ResourceEdge:
    arn = rid if rtype == ANY_RESOURCE_TYPE else None
    return ResourceEdge(
        rel, account, R, service, rtype, rid, to_arn=arn, confidence=confidence, evidence=evidence
    )  # type: ignore[arg-type]


def _res(
    rtype: str,
    rid: str,
    *,
    service: str = "ec2",
    account: str = A,
    arn: str | None = None,
    edges: list[ResourceEdge] | None = None,
    region: str = R,
    **state: Any,
) -> NormalizedResource:
    return NormalizedResource(
        account_id=account,
        region=region,
        service=service,
        resource_type=rtype,
        resource_id=rid,
        arn=arn,
        relationships=edges or [],
        observed_state={"resource_id": rid, **state},
        tags={"Name": state.get("name", rid)},
    )


def _persist(db: Session, items: list[NormalizedResource], account: str = A) -> None:
    acct = get_or_create_account(
        db, aws_account_id=account, project_alias="p", account_alias=f"a{account}"
    )
    run = create_inventory_run(
        db, account=acct, collector_name="test", collector_version="1", source="test"
    )
    upsert_resources(
        db,
        account=acct,
        run=run,
        resources=items,
        collector_version="1",
        source="test",
        collector_name="test.collector",
    )
    db.commit()


def _base_network() -> list[NormalizedResource]:
    return [
        _res("vpc", "vpc-1", cidr_block="10.0.0.0/16"),
        _res("subnet", "subnet-a", vpc_id="vpc-1", edges=[_edge("belongs_to", "vpc", "vpc-1")]),
        _res("subnet", "subnet-b", vpc_id="vpc-1", edges=[_edge("belongs_to", "vpc", "vpc-1")]),
        _res("security-group", "sg-shared", vpc_id="vpc-1"),
    ]


def _alb_ecs() -> list[NormalizedResource]:
    return _base_network() + [
        _res(
            "load-balancer",
            LB_ARN,
            service="elbv2",
            arn=LB_ARN,
            name="web",
            dns_name="web-123.us-east-1.elb.amazonaws.com",
            vpc_id="vpc-1",
            edges=[
                _edge("associated_with", "subnet", "subnet-a"),
                _edge("associated_with", "subnet", "subnet-b"),
            ],
        ),
        _res(
            "listener",
            L_ARN,
            service="elbv2",
            arn=L_ARN,
            edges=[
                _edge("belongs_to", "load-balancer", LB_ARN, service="elbv2"),
                _edge("forwards_to", "target-group", TG_ARN, service="elbv2"),
            ],
        ),
        _res("target-group", TG_ARN, service="elbv2", arn=TG_ARN, vpc_id="vpc-1", target_type="ip"),
        _res(
            "target-health",
            f"{TG_ARN}:10.0.1.5:80",
            service="elbv2",
            target_id="10.0.1.5",
            target_type="ip",
            target_port=80,
            health_state="healthy",
            edges=[_edge("belongs_to", "target-group", TG_ARN, service="elbv2")],
        ),
        _res(
            "network-interface",
            "eni-task",
            vpc_id="vpc-1",
            subnet_id="subnet-a",
            private_ip_address="10.0.1.5",
            edges=[
                _edge("located_in", "subnet", "subnet-a"),
                _edge("uses", "security-group", "sg-shared"),
            ],
        ),
        # Unrelated workload in the same VPC + SG: must NOT connect to the app.
        _res(
            "instance",
            "i-batch",
            vpc_id="vpc-1",
            subnet_id="subnet-b",
            edges=[
                _edge("located_in", "subnet", "subnet-b"),
                _edge("uses", "security-group", "sg-shared"),
            ],
        ),
    ]


def _doc(db: Session, **scope: Any) -> GraphDocument:
    return build_document(
        db, _config(), GraphScope(project_alias="shop", account_ids=[], **scope), max_nodes=400
    )


def _edges(doc: GraphDocument) -> set[tuple[str, str, str, str]]:
    by_id = {n.id: n for n in doc.nodes}
    return {
        (
            by_id[e.source].resource_id,
            e.relationship_type,
            by_id[e.target].resource_id,
            e.confidence,
        )
        for e in doc.edges
    }


def test_alb_to_ip_target_chain_and_inferred_eni(db: Session) -> None:
    _persist(db, _alb_ecs())
    doc = _doc(db, detail="full")
    edges = _edges(doc)
    assert (L_ARN, "belongs_to", LB_ARN, "verified") in edges
    assert (L_ARN, "forwards_to", TG_ARN, "verified") in edges
    # Registered IP matched to ENI: inferred, never verified.
    assert (TG_ARN, "routes_to", "eni-task", "inferred") in edges
    # target-health records are collapsed into the TG -> target edge.
    assert all(n.resource_type != "target-health" for n in doc.nodes)
    routes = next(e for e in doc.edges if e.relationship_type == "routes_to")
    assert routes.attributes == {"port": 80, "health_state": "healthy"}
    assert {ev.source_api for ev in routes.evidence} >= {
        "DescribeTargetHealth:Target.Id (ip)",
        "DescribeNetworkInterfaces:PrivateIpAddresses",
    }


def test_shared_vpc_or_security_group_never_links_workloads(db: Session) -> None:
    _persist(db, _alb_ecs())
    app = _doc(db, view="application", seed=f"{A}:{R}:load-balancer:{LB_ARN}")
    ids = {n.resource_id for n in app.nodes}
    assert {LB_ARN, L_ARN, TG_ARN, "eni-task"} <= ids
    assert "i-batch" not in ids
    for e in app.edges:
        assert {e.source, e.target} != {
            f"{A}:{R}:instance:i-batch",
            f"{A}:{R}:network-interface:eni-task",
        }


def test_vpcs_and_subnets_are_groups_with_placement(db: Session) -> None:
    _persist(db, _alb_ecs())
    doc = _doc(db, detail="full")
    assert not any(n.resource_type in {"vpc", "subnet"} for n in doc.nodes)
    groups = {g.id: g for g in doc.groups}
    vpc_group = groups[f"vpc:{A}:{R}:vpc-1"]
    assert vpc_group.node_id == f"{A}:{R}:vpc:vpc-1" and "10.0.0.0/16" in vpc_group.label
    lb = next(n for n in doc.nodes if n.resource_type == "load-balancer")
    assert lb.parent_group == vpc_group.id and lb.subnet_id is None  # spans 2 subnets
    eni = next(n for n in doc.nodes if n.resource_id == "eni-task")
    assert eni.parent_group == f"subnet:{A}:{R}:subnet-a"


def test_arn_keyed_targets_resolve_by_name_and_ambiguity_is_refused(db: Session) -> None:
    eks_arn = f"arn:aws:eks:{R}:{A}:cluster/prod"
    _persist(
        db,
        [
            _res("eks-cluster", eks_arn, service="eks", arn=eks_arn, name="prod"),
            _res(
                "eks-nodegroup",
                "prod/ng1",
                service="eks",
                edges=[_edge("belongs_to", "eks-cluster", "prod", service="eks")],
            ),
            _res("security-group", "sg-x", name="dup"),
            _res("security-group", "sg-y", name="dup"),
            _res("instance", "i-1", edges=[_edge("uses", "security-group", "dup")]),
        ],
    )
    doc = _doc(db, detail="full")
    assert ("prod/ng1", "belongs_to", eks_arn, "verified") in _edges(doc)
    unresolved = {(u.target_hint, u.reason) for u in doc.coverage.unresolved_references}
    assert ("dup", "ambiguous") in unresolved
    assert not any(e.relationship_type == "uses" for e in doc.edges)


def test_cloudfront_origin_and_route53_alias_matched_by_dns(db: Session) -> None:
    cf_arn = f"arn:aws:cloudfront::{A}:distribution/E1"
    _persist(
        db,
        _alb_ecs()
        + [
            _res(
                "cloudfront-distribution",
                cf_arn,
                service="cloudfront",
                arn=cf_arn,
                domain_name="d111.cloudfront.net",
                origins=[
                    {"id": "alb", "domain_name": "web-123.us-east-1.elb.amazonaws.com"},
                    {"id": "legacy", "domain_name": "origin.example.org"},
                ],
            ),
            _res(
                "route53-record",
                "Z1/www.shop.example./A/",
                service="route53",
                alias_target={"dns_name": "d111.cloudfront.net."},
            ),
            _res(
                "route53-record",
                "Z1/api.shop.example./A/",
                service="route53",
                alias_target={"dns_name": "dualstack.web-123.us-east-1.elb.amazonaws.com."},
            ),
        ],
    )
    edges = _edges(_doc(db))
    assert (cf_arn, "origin", LB_ARN, "verified") in edges
    assert ("Z1/www.shop.example./A/", "alias_to", cf_arn, "verified") in edges
    assert ("Z1/api.shop.example./A/", "alias_to", LB_ARN, "verified") in edges
    # A non-AWS origin is shown as an external endpoint, not an invented resource.
    assert (cf_arn, "origin", "origin.example.org", "verified") in edges
    doc = _doc(db)
    ext = next(n for n in doc.nodes if n.resource_id == "origin.example.org")
    assert ext.kind == "external" and ext.status == "not_collected"


def test_waf_any_type_arn_edges_and_security_category(db: Session) -> None:
    waf_arn = f"arn:aws:wafv2:{R}:{A}:regional/webacl/w/1"
    _persist(
        db,
        _alb_ecs()
        + [
            _res(
                "wafv2-web-acl",
                waf_arn,
                service="wafv2",
                arn=waf_arn,
                edges=[_edge("protects", ANY_RESOURCE_TYPE, LB_ARN, service="wafv2")],
            ),
        ],
    )
    doc = _doc(db)
    edge = next(e for e in doc.edges if e.relationship_type == "protects")
    assert edge.category == "security" and edge.confidence == "verified"


def test_account_isolation(db: Session) -> None:
    _persist(db, _alb_ecs())
    _persist(db, [_res("instance", "i-other", account=B)], account=B)
    doc = _doc(db)
    assert {n.account_id for n in doc.nodes} == {A}
    with pytest.raises(GraphScopeError):
        build_document(
            db, _config(), GraphScope(project_alias="shop", account_ids=[B]), max_nodes=10
        )
    assert node_detail(db, _config(), "shop", f"{B}:{R}:instance:i-other") is None
    # Edges pointing into an account outside the project are reported, not followed.
    _persist(
        db, [_res("instance", "i-x", edges=[_edge("uses", "security-group", "sg-b", account=B)])]
    )
    reasons = {u.reason for u in _doc(db).coverage.unresolved_references}
    assert "out_of_scope" in reasons


def test_inferred_edges_can_be_excluded_and_truncation_is_reported(db: Session) -> None:
    _persist(db, _alb_ecs())
    verified_only = _doc(db, detail="full", include_inferred=False)
    assert verified_only.stats.inferred_edges == 0
    small = build_document(
        db, _config(), GraphScope(project_alias="shop", account_ids=[]), max_nodes=2
    )
    assert small.stats.truncated and small.stats.node_count == 2
    assert small.stats.total_nodes_before_limit > 2


def test_exports_are_valid_and_mark_confidence(db: Session, tmp_path) -> None:
    _persist(db, _alb_ecs())
    doc = _doc(db, detail="full")
    mmd = to_mermaid(doc)
    assert mmd.splitlines()[0].startswith("%% CloudLens architecture")
    assert "flowchart LR" in mmd and "subgraph" in mmd
    assert "-.->|routes_to (inferred)|" in mmd and "-->|forwards_to|" in mmd
    assert "<br/><small>listener</small>" in mmd and "&lt;br" not in mmd
    svg = to_svg(doc)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert 'stroke-dasharray="6 4"' in svg  # inferred edge rendered dashed
    assert GraphDocument.model_validate_json(to_json(doc)).stats == doc.stats

    paths = write_artifacts(doc, tmp_path / "out")
    assert {p.suffix for p in paths} == {".json", ".mmd", ".svg"}
    for p in paths:
        assert p.parent == (tmp_path / "out" / "shop" / A).resolve()
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(paths[0].parent).st_mode) == 0o700


def test_artifact_paths_reject_traversal(db: Session, tmp_path) -> None:
    _persist(db, _alb_ecs())
    doc = _doc(db)
    doc.scope.project_alias = "../escape"
    with pytest.raises(ValueError):
        write_artifacts(doc, tmp_path)


def test_iam_action_mapping() -> None:
    assert iam_action_for("elbv2", "describe_rules") == "elasticloadbalancing:DescribeRules"
    assert iam_action_for("s3", "list_buckets") == "s3:ListAllMyBuckets"
    assert iam_action_for("apigatewayv2", "get_apis") == "apigateway:GET"
    assert iam_action_for("lambda", "list_functions") == "lambda:ListFunctions"


# ----------------------------------------------------------------- HTTP API


@pytest.fixture
def api(tmp_path) -> Iterator[TestClient]:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as s:
        _persist(s, _alb_ecs())
        _persist(s, [_res("instance", "i-other", account=B)], account=B)

    def _db() -> Iterator[Session]:
        with factory() as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[config_dep] = _config
    yield TestClient(app)
    engine.dispose()


def test_graph_document(api: TestClient) -> None:
    body = api.get("/graph", params={"project": "shop", "detail": "full"}).json()
    assert body["schema_version"] == "1"
    assert body["scope"]["account_ids"] == [A]
    assert body["stats"]["inferred_edges"] == 1
    assert body["assumptions"] and "coverage" in body
    assert all(n["account_id"] == A for n in body["nodes"])


def test_foreign_account_rejected(api: TestClient) -> None:
    r = api.get("/graph", params={"project": "shop", "account_id": B})
    assert r.status_code == 403
    r = api.get("/graph/node", params={"project": "shop", "id": f"{B}:{R}:instance:i-other"})
    assert r.status_code == 404
    assert api.get("/graph", params={"project": "nope"}).status_code == 403


def test_view_parameter_validation(api: TestClient) -> None:
    assert api.get("/graph", params={"project": "shop", "view": "vpc"}).status_code == 422
    assert api.get("/graph", params={"project": "shop", "view": "application"}).status_code == 422
    assert api.get("/graph", params={"project": "shop", "max_nodes": 99999}).status_code == 422


def test_node_detail_has_evidence(api: TestClient) -> None:
    r = api.get("/graph/node", params={"project": "shop", "id": f"{A}:{R}:load-balancer:{LB_ARN}"})
    assert r.status_code == 200
    body = r.json()
    assert body["observed_state"]["dns_name"].endswith("elb.amazonaws.com")
    assert body["inbound"][0]["evidence"][0]["source_api"]


def test_exports(api: TestClient) -> None:
    params = {"project": "shop", "view": "vpc", "vpc_id": "vpc-1"}
    mmd = api.get("/graph/export/mermaid", params=params)
    assert mmd.headers["content-type"].startswith("text/plain")
    assert "attachment" in mmd.headers["content-disposition"] and "flowchart" in mmd.text
    svg = api.get("/graph/export/svg", params=params)
    assert svg.headers["content-type"] == "image/svg+xml"
    assert "default-src 'none'" in svg.headers["content-security-policy"]
    js = api.get("/graph/export/json", params=params)
    assert js.json()["scope"]["vpc_id"] == "vpc-1"


def test_projects_summary(api: TestClient) -> None:
    projects = {p["project_alias"]: p for p in api.get("/graph/projects").json()}
    shop = projects["shop"]
    assert [v["vpc_id"] for v in shop["vpcs"]] == ["vpc-1"]
    assert any(e["resource_type"] == "load-balancer" for e in shop["entry_points"])
    assert all(e["account_id"] == A for e in shop["entry_points"])
