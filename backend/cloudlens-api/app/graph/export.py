"""Deterministic Mermaid / SVG / JSON renderings of a ``GraphDocument``.

Same document in, byte-identical output out (apart from the embedded
``generated_at``), so exports can be diffed and re-rendered.
"""

from __future__ import annotations

import re
from collections import defaultdict
from html import escape

from app.graph.models import GraphDocument, GraphEdge, GraphGroup, GraphNode

_SAFE = re.compile(r"[^A-Za-z0-9_]")


def _mid(raw: str, ids: dict[str, str]) -> str:
    """Short, Mermaid-safe identifier, stable for a given document."""
    if raw not in ids:
        ids[raw] = f"n{len(ids)}_{_SAFE.sub('_', raw)[-24:]}"
    return ids[raw]


def _mlabel(text: str) -> str:
    return escape(text, quote=True).replace("|", "&#124;").replace("\n", " ")


def _header(doc: GraphDocument) -> list[str]:
    s = doc.scope
    cov = doc.coverage
    return [
        f"CloudLens architecture: project={s.project_alias} view={s.view}",
        f"accounts={','.join(s.account_ids)} generated_at={doc.generated_at.isoformat()}",
        f"observations {cov.oldest_observation} .. {cov.newest_observation}",
        f"nodes={doc.stats.node_count} edges={doc.stats.edge_count} "
        f"verified={doc.stats.verified_edges} inferred={doc.stats.inferred_edges} "
        f"truncated={doc.stats.truncated}",
        f"missing_permissions={len(cov.missing_permissions)} "
        f"unresolved_references={len(cov.unresolved_references)}",
        "solid = verified from AWS API records; dashed = inferred; "
        "security associations are configuration, not traffic",
    ]


def to_mermaid(doc: GraphDocument) -> str:
    ids: dict[str, str] = {}
    children: dict[str | None, list[GraphGroup]] = defaultdict(list)
    for group in doc.groups:
        children[group.parent_id].append(group)
    nodes_in: dict[str | None, list[GraphNode]] = defaultdict(list)
    for node in doc.nodes:
        nodes_in[node.parent_group].append(node)

    lines = [f"%% {line}" for line in _header(doc)]
    lines.append("flowchart LR")

    def emit_node(node: GraphNode, indent: str) -> None:
        # Escape resource-provided text only; the <br/>/<small> markup is ours.
        label = f"{_mlabel(node.label)}<br/><small>{_mlabel(node.resource_type)}</small>"
        if node.kind == "external":
            lines.append(f'{indent}{_mid(node.id, ids)}[/"{label}"/]')
        else:
            lines.append(f'{indent}{_mid(node.id, ids)}["{label}"]')
        if node.context_only or node.kind == "external":
            lines.append(f"{indent}class {_mid(node.id, ids)} context")

    def emit_group(group: GraphGroup, depth: int) -> None:
        indent = "  " * depth
        lines.append(f'{indent}subgraph {_mid(group.id, ids)}["{_mlabel(group.label)}"]')
        for node in sorted(nodes_in.get(group.id, []), key=lambda n: n.id):
            emit_node(node, indent + "  ")
        for child in sorted(children.get(group.id, []), key=lambda g: g.id):
            emit_group(child, depth + 1)
        lines.append(f"{indent}end")

    for root in sorted(children.get(None, []), key=lambda g: g.id):
        emit_group(root, 1)
    for node in sorted(nodes_in.get(None, []), key=lambda n: n.id):
        emit_node(node, "  ")

    for edge in doc.edges:
        arrow = "-->" if edge.confidence == "verified" else "-.->"
        label = edge.relationship_type + ("" if edge.confidence == "verified" else " (inferred)")
        lines.append(
            f"  {_mid(edge.source, ids)} {arrow}|{_mlabel(label)}| {_mid(edge.target, ids)}"
        )
    lines.append("  classDef context stroke-dasharray: 4 3,opacity:0.7")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# SVG: simple layered layout (columns by flow depth, boxes per innermost group)
# --------------------------------------------------------------------------- #
_NODE_W, _NODE_H, _COL_GAP, _ROW_GAP, _PAD = 210, 46, 70, 14, 16
_CATEGORY_COLOR = {
    "flow": "#2563eb",
    "membership": "#7c3aed",
    "network": "#0f766e",
    "security": "#9ca3af",
    "dependency": "#b45309",
}


def _layers(doc: GraphDocument) -> dict[str, int]:
    """Longest-path depth over flow/membership edges (cycle-safe)."""
    depth = {n.id: 0 for n in doc.nodes}
    flow = [e for e in doc.edges if e.category in {"flow", "membership"}]
    for _ in range(len(doc.nodes)):
        changed = False
        for e in flow:
            if e.source in depth and e.target in depth and depth[e.target] < depth[e.source] + 1:
                if depth[e.source] + 1 < len(doc.nodes):
                    depth[e.target] = depth[e.source] + 1
                    changed = True
        if not changed:
            break
    return depth


def to_svg(doc: GraphDocument) -> str:
    depth = _layers(doc)
    by_group: dict[str | None, list[GraphNode]] = defaultdict(list)
    for node in doc.nodes:
        by_group[node.parent_group].append(node)
    group_label = {g.id: g for g in doc.groups}

    def chain(gid: str | None) -> str:
        parts = []
        while gid:
            g = group_label.get(gid)
            if g is None:
                break
            parts.append(g.label)
            gid = g.parent_id
        return " › ".join(reversed(parts)) or "Ungrouped"

    positions: dict[str, tuple[float, float]] = {}
    blocks: list[str] = []
    y = 120.0
    width = 800.0
    for gid in sorted(by_group, key=lambda g: chain(g)):
        members = sorted(by_group[gid], key=lambda n: (depth[n.id], n.resource_type, n.label))
        cols: dict[int, list[GraphNode]] = defaultdict(list)
        for n in members:
            cols[depth[n.id]].append(n)
        rows = max(len(v) for v in cols.values())
        ncols = max(cols) + 1
        box_h = _PAD * 2 + 22 + rows * (_NODE_H + _ROW_GAP)
        box_w = _PAD * 2 + ncols * (_NODE_W + _COL_GAP)
        width = max(width, box_w + 40)
        blocks.append(
            f'<rect x="20" y="{y}" width="{box_w}" height="{box_h}" rx="8" '
            f'fill="#f8fafc" stroke="#cbd5e1"/>'
            f'<text x="{20 + _PAD}" y="{y + 20}" class="g">{escape(chain(gid))}</text>'
        )
        for col, col_nodes in cols.items():
            for row, n in enumerate(col_nodes):
                nx_ = 20 + _PAD + col * (_NODE_W + _COL_GAP)
                ny = y + _PAD + 22 + row * (_NODE_H + _ROW_GAP)
                positions[n.id] = (nx_, ny)
                dash = ' stroke-dasharray="5 3"' if n.kind == "external" else ""
                opacity = ' opacity="0.6"' if n.context_only else ""
                blocks.append(
                    f'<g{opacity}><rect x="{nx_}" y="{ny}" width="{_NODE_W}" height="{_NODE_H}" '
                    f'rx="6" fill="#ffffff" stroke="#334155"{dash}/>'
                    f'<text x="{nx_ + 8}" y="{ny + 19}" class="l">{escape(n.label[:34])}</text>'
                    f'<text x="{nx_ + 8}" y="{ny + 35}" class="t">{escape(n.resource_type)}'
                    f"{' · stale' if n.is_stale else ''}</text>"
                    f"<title>{escape(n.id)}</title></g>"
                )
        y += box_h + 24

    edge_svg: list[str] = []
    for e in doc.edges:
        if e.source not in positions or e.target not in positions:
            continue
        sx, sy = positions[e.source]
        tx, ty = positions[e.target]
        x1, y1 = sx + _NODE_W, sy + _NODE_H / 2
        x2, y2 = tx, ty + _NODE_H / 2
        if tx <= sx:
            x1, x2 = sx + _NODE_W / 2, tx + _NODE_W / 2
            y1, y2 = sy + _NODE_H, ty
        dash = "" if e.confidence == "verified" else ' stroke-dasharray="6 4"'
        color = _CATEGORY_COLOR.get(e.category, "#475569")
        edge_svg.append(
            f'<path d="M{x1},{y1} C{(x1 + x2) / 2},{y1} {(x1 + x2) / 2},{y2} {x2},{y2}" '
            f'fill="none" stroke="{color}" stroke-width="1.4"{dash} marker-end="url(#a)">'
            f"<title>{escape(e.relationship_type)} ({e.confidence})</title></path>"
        )

    header = "".join(
        f'<text x="20" y="{24 + i * 16}" class="{"h" if i == 0 else "m"}">{escape(line)}</text>'
        for i, line in enumerate(_header(doc))
    )
    height = y + 20
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Helvetica, Arial, sans-serif">'
        "<style>.h{font-size:15px;font-weight:600}.m{font-size:11px;fill:#475569}"
        ".g{font-size:12px;font-weight:600;fill:#334155}.l{font-size:12px}"
        ".t{font-size:10px;fill:#64748b}</style>"
        '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" '
        'fill="#475569"/></marker></defs>'
        f'<rect width="100%" height="100%" fill="#ffffff"/>{header}'
        + "".join(blocks)
        + "".join(edge_svg)
        + "</svg>\n"
    )


def to_json(doc: GraphDocument) -> str:
    return doc.model_dump_json(indent=2) + "\n"


def edge_index(doc: GraphDocument) -> dict[str, list[GraphEdge]]:
    out: dict[str, list[GraphEdge]] = defaultdict(list)
    for e in doc.edges:
        out[e.source].append(e)
    return out
