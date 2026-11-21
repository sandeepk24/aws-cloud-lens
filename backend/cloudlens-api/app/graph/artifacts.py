"""Write graph exports to a private, per-project/per-account location."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path

from app.graph.export import to_json, to_mermaid, to_svg
from app.graph.models import GraphDocument

_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
FORMATS = ("json", "mmd", "svg")


def _safe(component: str) -> str:
    if not _COMPONENT.match(component) or component in {".", ".."}:
        raise ValueError(f"Unsafe path component: {component!r}")
    return component


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def write_artifacts(
    doc: GraphDocument,
    base_dir: str | Path,
    *,
    formats: tuple[str, ...] = FORMATS,
    now: datetime | None = None,
) -> list[Path]:
    """Write ``doc`` once per account directory it covers.

    Multi-account documents go under ``<project>/_multi-account/`` so a single
    account's folder only ever holds that account's data.
    """
    unknown = set(formats) - set(FORMATS)
    if unknown:
        raise ValueError(f"Unsupported formats: {sorted(unknown)}")
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    scope = doc.scope
    account_dir = _safe(scope.account_ids[0]) if len(scope.account_ids) == 1 else "_multi-account"
    qualifier = scope.vpc_id or scope.service or (scope.seed and "seed") or scope.application_tag
    name = "-".join(
        _safe(re.sub(r"[^A-Za-z0-9._-]", "_", part))
        for part in (scope.view, qualifier, stamp)
        if part
    )
    root = Path(base_dir).expanduser().resolve()
    target = _private_dir(_private_dir(root / _safe(scope.project_alias)) / account_dir)
    renderers = {"json": to_json, "mmd": to_mermaid, "svg": to_svg}
    written: list[Path] = []
    for fmt in formats:
        path = target / f"{name}.{fmt}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(renderers[fmt](doc))
        written.append(path)
    return written
