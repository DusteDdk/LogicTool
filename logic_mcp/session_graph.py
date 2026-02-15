from __future__ import annotations

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


TYPE_ORDER = ["bundle", "rule", "expectation", "concept", "code_binding"]


def build_session_graph_table(session_id: str) -> dict[str, Any] | None:
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

    return _render_table(safe_session_id, list(nodes.values()), edges)


def _type_sort_key(node_type: str) -> int:
    if node_type in TYPE_ORDER:
        return TYPE_ORDER.index(node_type)
    return len(TYPE_ORDER)


def _render_table(session_id: str, nodes: list[GraphNode], edges: list[GraphEdge]) -> dict[str, Any]:
    node_ids = {node.node_id for node in nodes}
    outgoing: dict[str, list[dict[str, str]]] = {node.node_id: [] for node in nodes}
    incoming_count: dict[str, int] = {node.node_id: 0 for node in nodes}
    seen_edges: set[tuple[str, str, str]] = set()

    for edge in edges:
        label = edge.label if isinstance(edge.label, str) else ""
        key = (edge.source_id, edge.target_id, label)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        if edge.source_id not in node_ids or edge.target_id not in node_ids:
            continue
        outgoing[edge.source_id].append({"label": label, "target_id": edge.target_id})
        incoming_count[edge.target_id] += 1

    rows: list[dict[str, Any]] = []
    for node in sorted(nodes, key=lambda n: (_type_sort_key(n.node_type), n.node_id)):
        relations = outgoing.get(node.node_id, [])
        relations.sort(key=lambda rel: (str(rel.get("label", "")), str(rel.get("target_id", ""))))
        rows.append(
            {
                "type": node.node_type,
                "id": node.node_id,
                "outgoing_relations": relations,
                "incoming_relations_count": incoming_count.get(node.node_id, 0),
            }
        )

    return {
        "session_id": session_id,
        "row_count": len(rows),
        "rows": rows,
    }
