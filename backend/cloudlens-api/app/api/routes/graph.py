"""Read-only architecture graph endpoints.

All data comes from persisted inventory. Requests are scoped to accounts that
belong to the requested project in the accounts configuration; anything else
is rejected. No endpoint triggers AWS API calls.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.accounts import AccountsConfig
from app.accounts.loader import AccountsConfigError, get_accounts_config
from app.db.session import get_db
from app.graph.export import to_json, to_mermaid, to_svg
from app.graph.models import GraphDocument, GraphScope, NodeDetail, ProjectSummary
from app.graph.service import (
    DEFAULT_MAX_NODES,
    HARD_MAX_NODES,
    GraphScopeError,
    build_document,
    node_detail,
    project_summaries,
)

router = APIRouter(prefix="/graph", tags=["graph"])


def _config() -> AccountsConfig:
    try:
        return get_accounts_config()
    except AccountsConfigError as exc:
        raise HTTPException(status_code=503, detail="Accounts configuration unavailable") from exc


ConfigDep = Annotated[AccountsConfig, Depends(_config)]
DbDep = Annotated[Session, Depends(get_db)]


def _scope(
    project: Annotated[str, Query(min_length=1, max_length=128)],
    account_id: Annotated[list[str] | None, Query(max_length=50)] = None,
    region: Annotated[list[str] | None, Query(max_length=50)] = None,
    view: Literal["account", "vpc", "application", "service"] = "account",
    vpc_id: Annotated[str | None, Query(max_length=64)] = None,
    service: Annotated[str | None, Query(max_length=64)] = None,
    seed: Annotated[str | None, Query(max_length=2600)] = None,
    app_tag: Annotated[str | None, Query(max_length=512)] = None,
    include_inferred: bool = True,
    include_stale: bool = False,
    detail: Literal["standard", "full"] = "standard",
) -> GraphScope:
    if view == "vpc" and not vpc_id:
        raise HTTPException(status_code=422, detail="view=vpc requires vpc_id")
    if view == "service" and not service:
        raise HTTPException(status_code=422, detail="view=service requires service")
    if view == "application" and not (seed or app_tag):
        raise HTTPException(status_code=422, detail="view=application requires seed or app_tag")
    return GraphScope(
        project_alias=project,
        account_ids=account_id or [],
        regions=region or [],
        view=view,
        vpc_id=vpc_id,
        service=service,
        seed=seed,
        application_tag=app_tag,
        include_inferred=include_inferred,
        include_stale=include_stale,
        detail=detail,
    )


ScopeDep = Annotated[GraphScope, Depends(_scope)]
MaxNodes = Annotated[int, Query(ge=1, le=HARD_MAX_NODES)]


def _document(
    db: Session, config: AccountsConfig, scope: GraphScope, max_nodes: int
) -> GraphDocument:
    try:
        return build_document(db, config, scope, max_nodes=max_nodes)
    except GraphScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _filename(doc: GraphDocument, ext: str) -> str:
    accounts = doc.scope.account_ids[0] if len(doc.scope.account_ids) == 1 else "multi"
    stamp = doc.generated_at.strftime("%Y%m%dT%H%M%SZ")
    return f"cloudlens-{doc.scope.project_alias}-{accounts}-{doc.scope.view}-{stamp}.{ext}"


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects(db: DbDep, config: ConfigDep) -> list[ProjectSummary]:
    """Projects, accounts, VPCs, services, entry points and app tags for pickers."""
    return project_summaries(db, config)


@router.get("", response_model=GraphDocument)
def get_graph(
    db: DbDep, config: ConfigDep, scope: ScopeDep, max_nodes: MaxNodes = DEFAULT_MAX_NODES
) -> GraphDocument:
    """Architecture graph for one view, with coverage and assumptions."""
    return _document(db, config, scope, max_nodes)


@router.get("/node", response_model=NodeDetail)
def get_node(
    db: DbDep,
    config: ConfigDep,
    project: Annotated[str, Query(min_length=1, max_length=128)],
    id: Annotated[str, Query(min_length=1, max_length=2600)],  # noqa: A002
) -> NodeDetail:
    try:
        detail = node_detail(db, config, project, id)
    except GraphScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Node not found in this project")
    return detail


@router.get("/export/mermaid", response_class=Response)
def export_mermaid(
    db: DbDep, config: ConfigDep, scope: ScopeDep, max_nodes: MaxNodes = DEFAULT_MAX_NODES
) -> Response:
    doc = _document(db, config, scope, max_nodes)
    return Response(
        to_mermaid(doc),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_filename(doc, "mmd")}"'},
    )


@router.get("/export/svg", response_class=Response)
def export_svg(
    db: DbDep, config: ConfigDep, scope: ScopeDep, max_nodes: MaxNodes = DEFAULT_MAX_NODES
) -> Response:
    doc = _document(db, config, scope, max_nodes)
    return Response(
        to_svg(doc),
        media_type="image/svg+xml",
        headers={
            "Content-Disposition": f'attachment; filename="{_filename(doc, "svg")}"',
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


@router.get("/export/json", response_class=Response)
def export_json(
    db: DbDep, config: ConfigDep, scope: ScopeDep, max_nodes: MaxNodes = DEFAULT_MAX_NODES
) -> Response:
    doc = _document(db, config, scope, max_nodes)
    return Response(
        to_json(doc),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{_filename(doc, "json")}"'},
    )
