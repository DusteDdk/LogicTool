from __future__ import annotations

import html
import math
from dataclasses import dataclass
from typing import Any

from .store import Store
from .store import sanitize_namespace


@dataclass
class GraphNode:
    node_id: str
    node_type: str
    label: str
    details: list[str]


@dataclass
class GraphEdge:
    source_id: str
    target_id: str
    label: str


NODE_COLORS = {
    "bundle": "#4f46e5",
    "rule": "#0ea5e9",
    "expectation": "#f59e0b",
    "concept": "#10b981",
    "code_binding": "#8b5cf6",
}

TYPE_ORDER = ["bundle", "rule", "expectation", "concept", "code_binding"]


def build_session_graph_svg(session_id: str) -> str | None:
    safe_session_id = sanitize_namespace(session_id)
    store = Store(safe_session_id)
    data = store.data if isinstance(store.data, dict) else {}
    active_bundles = store.get_active_items("bundles")
    active_rules = store.get_active_items("rules")
    active_expectations = store.get_active_items("expectations")
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    concepts = context.get("concepts") if isinstance(context.get("concepts"), dict) else {}
    code_bindings = context.get("code_bindings") if isinstance(context.get("code_bindings"), dict) else {}

    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []

    for item_id, payload in active_bundles.items():
        nodes[item_id] = GraphNode(
            node_id=item_id,
            node_type="bundle",
            label=item_id,
            details=[f"type: bundle", f"lang: {payload.get('lang', '')}".strip()],
        )

    for item_id, payload in active_rules.items():
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        details = [f"type: rule", f"lang: {payload.get('lang', '')}".strip()]
        title = meta.get("title")
        if isinstance(title, str) and title:
            details.append(f"title: {title}")
        nodes[item_id] = GraphNode(node_id=item_id, node_type="rule", label=item_id, details=details)

    for item_id, payload in active_expectations.items():
        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        kind = content.get("kind")
        a_ref = content.get("a_ref")
        b_ref = content.get("b_ref")
        nodes[item_id] = GraphNode(
            node_id=item_id,
            node_type="expectation",
            label=item_id,
            details=[
                "type: expectation",
                f"kind: {kind}" if isinstance(kind, str) else "kind: unknown",
                f"a_ref: {a_ref}" if isinstance(a_ref, str) else "a_ref: unknown",
                f"b_ref: {b_ref}" if isinstance(b_ref, str) else "b_ref: unknown",
            ],
        )
        if isinstance(a_ref, str) and isinstance(b_ref, str) and a_ref in nodes and b_ref in nodes:
            edge_label = f"expects:{kind}" if isinstance(kind, str) and kind else "expects"
            edges.append(GraphEdge(source_id=a_ref, target_id=b_ref, label=edge_label))

    for item_id, payload in concepts.items():
        if not isinstance(payload, dict):
            continue
        concept_name = payload.get("concept")
        meaning = payload.get("meaning")
        nodes[item_id] = GraphNode(
            node_id=item_id,
            node_type="concept",
            label=item_id,
            details=[
                f"type: concept",
                f"concept: {concept_name}" if isinstance(concept_name, str) else "concept: unknown",
                f"meaning: {meaning}" if isinstance(meaning, str) else "meaning: unknown",
            ],
        )

    for item_id, payload in code_bindings.items():
        if not isinstance(payload, dict):
            continue
        path = payload.get("path")
        kind = payload.get("kind", "source")
        behavior = payload.get("function_or_behavior")
        details = [
            "type: code_binding",
            f"path: {path}" if isinstance(path, str) else "path: unknown",
            f"kind: {kind}" if isinstance(kind, str) else "kind: source",
        ]
        if isinstance(behavior, str) and behavior:
            details.append(f"behavior: {behavior}")
        nodes[item_id] = GraphNode(
            node_id=item_id,
            node_type="code_binding",
            label=item_id,
            details=details,
        )

    for item_id, payload in active_rules.items():
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        bundle_ids = meta.get("bundle_ids")
        if not isinstance(bundle_ids, list):
            continue
        for bundle_id in bundle_ids:
            if isinstance(bundle_id, str) and item_id in nodes and bundle_id in nodes:
                edges.append(GraphEdge(source_id=item_id, target_id=bundle_id, label="uses_bundle"))

    for item_id, payload in concepts.items():
        if not isinstance(payload, dict) or item_id not in nodes:
            continue
        for ref in payload.get("related_rule_ids", []):
            if isinstance(ref, str) and ref in nodes:
                edges.append(GraphEdge(source_id=item_id, target_id=ref, label="related_rule"))
        for ref in payload.get("related_expectation_ids", []):
            if isinstance(ref, str) and ref in nodes:
                edges.append(GraphEdge(source_id=item_id, target_id=ref, label="related_expectation"))
        for ref in payload.get("related_code_binding_ids", []):
            if isinstance(ref, str) and ref in nodes:
                edges.append(GraphEdge(source_id=item_id, target_id=ref, label="related_code"))

    for item_id, payload in code_bindings.items():
        if not isinstance(payload, dict) or item_id not in nodes:
            continue
        for ref in payload.get("related_rule_ids", []):
            if isinstance(ref, str) and ref in nodes:
                edges.append(GraphEdge(source_id=item_id, target_id=ref, label="related_rule"))
        for ref in payload.get("related_expectation_ids", []):
            if isinstance(ref, str) and ref in nodes:
                edges.append(GraphEdge(source_id=item_id, target_id=ref, label="related_expectation"))
        for ref in payload.get("related_concept_ids", []):
            if isinstance(ref, str) and ref in nodes:
                edges.append(GraphEdge(source_id=item_id, target_id=ref, label="related_concept"))

    return _render_svg(safe_session_id, list(nodes.values()), edges)


def _render_svg(session_id: str, nodes: list[GraphNode], edges: list[GraphEdge]) -> str | None:
    width = 1200
    height = 1200
    header_h = 56
    footer_h = 20

    by_type: dict[str, list[GraphNode]] = {node_type: [] for node_type in TYPE_ORDER}
    for node in sorted(nodes, key=lambda n: (TYPE_ORDER.index(n.node_type), n.node_id)):
        by_type[node.node_type].append(node)
    row_spacing = 95
    max_rows = max(1, max(len(items) for items in by_type.values()))
    content_height = max(1, height - header_h - footer_h)
    row_spacing = min(row_spacing, max(1, int(content_height / max_rows)))
    column_spacing = int(width / (len(TYPE_ORDER) + 1))

    positions: dict[str, tuple[float, float]] = {}
    for idx, node_type in enumerate(TYPE_ORDER):
        items = by_type[node_type]
        if not items:
            continue
        x = (idx + 1) * column_spacing
        top_pad = header_h + 22
        if len(items) > 1:
            y_step = (height - top_pad - 32) / (len(items) - 1)
            for i, node in enumerate(items):
                positions[node.node_id] = (float(x), float(top_pad + i * y_step))
        else:
            positions[items[0].node_id] = (float(x), float((top_pad + height - 32) / 2))

    degrees: dict[str, int] = {node.node_id: 0 for node in nodes}
    for edge in edges:
        if edge.source_id in degrees:
            degrees[edge.source_id] += 1
        if edge.target_id in degrees:
            degrees[edge.target_id] += 1

    svg: list[str] = []
    svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" role="img">')
    svg.append("<defs>")
    svg.append(
        "<marker id='arrow' markerWidth='12' markerHeight='8' refX='10' refY='4' orient='auto' markerUnits='strokeWidth'>"
        "<path d='M0,0 L12,4 L0,8 z' fill='#94a3b8'/></marker>"
    )
    svg.append("</defs>")
    svg.append(f"<rect x='1' y='1' width='{width - 2}' height='{height - 2}' rx='6' ry='6' fill='#ffffff' stroke='#d1d5db'/>")
    svg.append(f"<text x='18' y='30' font-size='17' fill='#111827'>Session graph: {html.escape(session_id)}</text>")
    if not nodes:
        svg.append(
            f"<text x='{width / 2:.2f}' y='{height / 2:.2f}' text-anchor='middle' dominant-baseline='middle' "
            "font-size='24' fill='#6b7280'>[no logic]</text>"
        )

    for edge in edges:
        src = positions.get(edge.source_id)
        dst = positions.get(edge.target_id)
        if src is None or dst is None:
            continue
        x1, y1 = src
        x2, y2 = dst
        svg.append(
            f"<line x1='{x1:.2f}' y1='{y1:.2f}' x2='{x2:.2f}' y2='{y2:.2f}' stroke='#94a3b8' stroke-width='1.5' marker-end='url(#arrow)'/>"
        )
        mid_x = (x1 + x2) / 2
        mid_y = (y1 + y2) / 2
        label = html.escape(edge.label)
        if label:
            svg.append(f"<text x='{mid_x:.2f}' y='{mid_y - 4:.2f}' font-size='10' fill='#6b7280'>{label}</text>")

    for node in nodes:
        pos = positions.get(node.node_id)
        if pos is None:
            continue
        x, y = pos
        degree = degrees.get(node.node_id, 0)
        radius = 16 + min(16, math.sqrt(max(0, degree)) * 4)
        color = NODE_COLORS.get(node.node_type, "#64748b")
        tooltip = html.escape("\n".join([node.label, f"type: {node.node_type}", f"direct_refs: {degree}", *node.details]))
        text = html.escape(node.label if len(node.label) <= 30 else f"{node.label[:27]}...")
        svg.append(
            f"<g><circle cx='{x:.2f}' cy='{y:.2f}' r='{radius:.2f}' fill='{color}' fill-opacity='0.86' stroke='#1f2937' stroke-width='1'>"
            f"<title>{tooltip}</title></circle>"
            f"<text x='{x:.2f}' y='{y + radius + 14:.2f}' text-anchor='middle' font-size='11' fill='#111827'>{text}</text></g>"
        )

    svg.append("</svg>")
    return "".join(svg)
