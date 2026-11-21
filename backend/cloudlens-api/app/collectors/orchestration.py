"""Reusable paginated collection orchestration.

Features:
- page iteration with next-token pagination
- bounded concurrency across regions
- retries with backoff for retryable failures
- resource upserts and relationship upserts
- stale resource marking only after a fully successful scope collection
  (a partial run never saw the remaining pages, so absence is not evidence)
- per-run timing / page stats persisted to ``InventoryRun.stats``
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.collectors.base import PaginatedCollector
from app.collectors.normalized import CollectorError, NormalizedResource, ResourcePage
from app.connectors.aws.retry import call_with_backoff, is_retryable_aws_error
from app.db.models import InventoryRunStatus
from app.db.repository import (
    create_inventory_run,
    finish_inventory_run,
    get_or_create_account,
    mark_stale_resources,
    record_collection_error,
    upsert_relationships_batch,
    upsert_resources,
)

logger = logging.getLogger(__name__)

_account_bootstrap_guards: dict[str, threading.Lock] = {}
_account_bootstrap_meta = threading.Lock()


def _account_bootstrap_lock(aws_account_id: str) -> threading.Lock:
    with _account_bootstrap_meta:
        if aws_account_id not in _account_bootstrap_guards:
            _account_bootstrap_guards[aws_account_id] = threading.Lock()
        return _account_bootstrap_guards[aws_account_id]


@dataclass(slots=True)
class CollectionScope:
    """Account/region scope for a collection job (values from external config)."""

    aws_account_id: str
    project_alias: str
    account_alias: str
    region: str


@dataclass(slots=True)
class OrchestratorConfig:
    max_workers: int = 4
    max_retries: int = 5
    mark_stale: bool = True
    source_override: str | None = None


@dataclass(slots=True)
class ScopeResult:
    region: str
    run_id: str
    status: InventoryRunStatus
    resources_seen: int = 0
    resources_stale: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class OrchestrationResult:
    collector_name: str
    scopes: list[ScopeResult] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return bool(self.scopes) and all(
            s.status == InventoryRunStatus.SUCCEEDED for s in self.scopes
        )


def _is_retryable_collector_error(exc: BaseException) -> bool:
    if isinstance(exc, CollectorError):
        return exc.retryable
    return is_retryable_aws_error(exc)


class CollectionOrchestrator:
    """Coordinate paginated collectors with persistence and concurrency."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        config: OrchestratorConfig | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config or OrchestratorConfig()
        self._sleep = sleep

    def collect(
        self,
        collector: PaginatedCollector,
        scopes: list[CollectionScope],
    ) -> OrchestrationResult:
        if not scopes:
            return OrchestrationResult(collector_name=collector.name)

        result = OrchestrationResult(collector_name=collector.name)
        workers = max(1, min(self._config.max_workers, len(scopes)))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._collect_scope, collector, scope): scope
                for scope in scopes
            }
            for future in as_completed(futures):
                scope = futures[future]
                try:
                    result.scopes.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Unhandled orchestration failure for account=%s region=%s",
                        scope.aws_account_id,
                        scope.region,
                    )
                    result.scopes.append(
                        ScopeResult(
                            region=scope.region,
                            run_id="",
                            status=InventoryRunStatus.FAILED,
                            error_count=1,
                            errors=[str(exc)],
                        )
                    )
        return result

    def _collect_scope(
        self, collector: PaginatedCollector, scope: CollectionScope
    ) -> ScopeResult:
        session = self._session_factory()
        try:
            source = self._config.source_override or collector.source
            with _account_bootstrap_lock(scope.aws_account_id):
                account = get_or_create_account(
                    session,
                    aws_account_id=scope.aws_account_id,
                    project_alias=scope.project_alias,
                    account_alias=scope.account_alias,
                )
                run = create_inventory_run(
                    session,
                    account=account,
                    collector_name=collector.name,
                    collector_version=collector.version,
                    source=source,
                    region=scope.region,
                    service=collector.service,
                    resource_type=collector.resource_type,
                )
                session.commit()
                # Detach identity fields so later work does not depend on expired ORM state.
                account_pk = account.id
                run_id = run.id

            account = session.get(type(account), account_pk)
            run = session.get(type(run), run_id)
            if account is None or run is None:
                raise RuntimeError("Failed to reload inventory run after bootstrap")

            seen: set[str] = set()
            page_errors: list[str] = []
            next_token: str | None = None
            started = time.perf_counter()
            fetch_seconds = 0.0
            persist_seconds = 0.0
            pages = 0
            soft_errors = 0

            while True:
                fetch_started = time.perf_counter()
                try:
                    page = self._fetch_page_with_retry(
                        collector,
                        account_id=scope.aws_account_id,
                        region=scope.region,
                        next_token=next_token,
                    )
                except Exception as exc:  # noqa: BLE001
                    record_collection_error(
                        session,
                        run=run,
                        account=account,
                        collector_name=collector.name,
                        message=str(exc),
                        error_code=getattr(exc, "error_code", None),
                        retryable=_is_retryable_collector_error(exc),
                        region=scope.region,
                        service=collector.service,
                        resource_type=collector.resource_type,
                        details={"phase": "fetch_page", "next_token": next_token},
                    )
                    page_errors.append(str(exc))
                    session.commit()
                    fetch_seconds += time.perf_counter() - fetch_started
                    break
                fetch_seconds += time.perf_counter() - fetch_started
                pages += 1

                persist_started = time.perf_counter()
                items = [
                    item
                    for item in page.items
                    if self._item_matches_scope(item, scope, collector)
                ]
                keys = upsert_resources(
                    session,
                    account=account,
                    run=run,
                    resources=items,
                    collector_version=collector.version,
                    source=source,
                    collector_name=collector.name,
                    source_api=getattr(collector, "describe_method", None),
                )
                seen.update(keys)
                drain = getattr(collector, "drain_soft_failures", None)
                for soft in drain(scope.region) if callable(drain) else []:
                    record_collection_error(
                        session,
                        run=run,
                        account=account,
                        collector_name=collector.name,
                        message=str(soft),
                        error_code=soft.error_code,
                        retryable=soft.retryable,
                        region=scope.region,
                        service=collector.service,
                        resource_type=collector.resource_type,
                        details={"phase": "item", **soft.details},
                    )
                    soft_errors += 1

                upsert_relationships_batch(
                    session,
                    resources=items,
                    collector_version=collector.version,
                    source=source,
                )

                session.commit()
                persist_seconds += time.perf_counter() - persist_started
                next_token = page.next_token
                if not next_token:
                    break

            status = InventoryRunStatus.SUCCEEDED
            if page_errors and seen:
                status = InventoryRunStatus.PARTIAL
            elif page_errors and not seen:
                status = InventoryRunStatus.FAILED
            elif soft_errors:
                # Listed fine, but some per-item details were denied/missing:
                # the scan is incomplete, so do not treat absence as deletion.
                status = InventoryRunStatus.PARTIAL

            stale_count = 0
            # Only a complete pass is evidence of absence. On PARTIAL runs the
            # pages after the failure were never fetched, so marking unseen
            # resources stale would report live resources as gone.
            if self._config.mark_stale and status == InventoryRunStatus.SUCCEEDED:
                stale_count = mark_stale_resources(
                    session,
                    account=account,
                    run=run,
                    region=scope.region,
                    service=collector.service,
                    resource_type=collector.resource_type,
                    seen_identity_keys=None,
                    observed_at=datetime.now(UTC),
                )

            run.stats = {
                "pages": pages,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "fetch_ms": round(fetch_seconds * 1000, 1),
                "persist_ms": round(persist_seconds * 1000, 1),
                "stale_marking": (
                    "applied"
                    if self._config.mark_stale and status == InventoryRunStatus.SUCCEEDED
                    else "skipped"
                ),
            }

            finish_inventory_run(
                session,
                run,
                status=status,
                message="; ".join(page_errors) if page_errors else None,
            )
            session.commit()

            return ScopeResult(
                region=scope.region,
                run_id=run.id,
                status=status,
                resources_seen=int(run.resources_seen or 0),
                resources_stale=stale_count,
                error_count=int(run.error_count or 0),
                errors=page_errors,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _fetch_page_with_retry(
        self,
        collector: PaginatedCollector,
        *,
        account_id: str,
        region: str,
        next_token: str | None,
    ) -> ResourcePage:
        kwargs: dict[str, Any] = {
            "max_attempts": self._config.max_retries,
            "is_retryable": _is_retryable_collector_error,
        }
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep

        return call_with_backoff(
            lambda: collector.fetch_page(
                account_id=account_id,
                region=region,
                next_token=next_token,
            ),
            **kwargs,
        )

    @staticmethod
    def _item_matches_scope(
        item: NormalizedResource,
        scope: CollectionScope,
        collector: PaginatedCollector,
    ) -> bool:
        return (
            item.account_id == scope.aws_account_id
            and item.region == scope.region
            and item.service == collector.service
            and item.resource_type == collector.resource_type
        )
