from __future__ import annotations

import unittest

from logic_mcp.session_graph import GraphEdge
from logic_mcp.session_graph import GraphNode
from logic_mcp.session_graph import _render_table


class SessionGraphRenderTests(unittest.TestCase):
    def test_render_table_returns_empty_rows_when_no_nodes(self) -> None:
        table = _render_table("default", [], [])
        self.assertIsInstance(table, dict)
        self.assertEqual(table.get("session_id"), "default")
        self.assertEqual(table.get("row_count"), 0)
        self.assertEqual(table.get("rows"), [])

    def test_render_table_lists_relations_and_incoming_count(self) -> None:
        nodes = [
            GraphNode(
                node_id="r_a",
                node_type="rule",
                label="r_a",
                details=["type: rule"],
                version=2,
                created_at=1771100000.0,
                content_lang="pyexpr",
                content_value="x > 0",
                content_bytes=7,
            ),
            GraphNode(node_id="b_a", node_type="bundle", label="b_a", details=["type: bundle"]),
            GraphNode(node_id="c_a", node_type="concept", label="c_a", details=["type: concept"]),
        ]
        edges = [
            GraphEdge(source_id="r_a", target_id="b_a", label="uses_bundle"),
            GraphEdge(source_id="c_a", target_id="r_a", label="related_rule"),
        ]
        table = _render_table("default", nodes, edges)
        self.assertIsInstance(table, dict)
        rows = table.get("rows", [])
        self.assertEqual(table.get("row_count"), 3)
        self.assertEqual(rows[0].get("id"), "b_a")
        by_id = {row.get("id"): row for row in rows}
        self.assertEqual(by_id["r_a"].get("incoming_relations_count"), 1)
        self.assertEqual(by_id["b_a"].get("incoming_relations_count"), 1)
        self.assertEqual(by_id["c_a"].get("incoming_relations_count"), 0)
        self.assertEqual(
            by_id["r_a"].get("incoming_relations"),
            [{"label": "related_rule", "source_id": "c_a"}],
        )
        self.assertEqual(
            by_id["r_a"].get("outgoing_relations"),
            [{"label": "uses_bundle", "target_id": "b_a"}],
        )
        self.assertEqual(by_id["r_a"].get("version"), 2)
        self.assertEqual(by_id["r_a"].get("content_lang"), "pyexpr")
        self.assertEqual(by_id["r_a"].get("content_value"), "x > 0")
        self.assertEqual(by_id["r_a"].get("content_bytes"), 7)


if __name__ == "__main__":
    unittest.main()
