from __future__ import annotations

import shutil
import unittest
import uuid

from logic_mcp.engine import LogicEngine
from logic_mcp.errors import LogicError
from logic_mcp.paths import STORE_DIR
from logic_mcp.store import sanitize_namespace


class LogicEngineV5Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.session_id = f"ut-{uuid.uuid4().hex[:10]}"
        safe = sanitize_namespace(self.session_id)
        self.session_dir = STORE_DIR / safe
        self.log_file = STORE_DIR / f"{safe}_log.jsonl"
        shutil.rmtree(self.session_dir, ignore_errors=True)
        if self.log_file.exists():
            self.log_file.unlink()
        self.engine = LogicEngine(self.session_id)

    def tearDown(self) -> None:
        shutil.rmtree(self.session_dir, ignore_errors=True)
        if self.log_file.exists():
            self.log_file.unlink()

    def test_set_rule_and_unified_list_flow(self) -> None:
        out = self.engine.list_items({})
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"], {"items": []})

        self.assertEqual(self.engine.set_rule({"id": "r1", "lang": "pyexpr", "rule": "x > 0"}), {"ok": True})

        listed = self.engine.list_items({"show": ["rules"], "detail_level": "full"})
        self.assertTrue(listed["ok"])
        self.assertEqual(len(listed["result"]["items"]), 1)
        item = listed["result"]["items"][0]
        self.assertEqual(item["id"], "r1")
        self.assertEqual(item["type"], "rule")
        self.assertIn("content", item)

        self.assertEqual(self.engine.remove_rule({"id": "r1"}), {"ok": True})
        listed_after = self.engine.list_items({"show": ["rules"]})
        self.assertEqual(listed_after["result"], {"items": []})

    def test_global_id_uniqueness_across_types(self) -> None:
        self.engine.set_rule({"id": "dup", "lang": "pyexpr", "rule": "x > 0"})
        with self.assertRaises(LogicError) as err:
            self.engine.context_patch(
                {
                    "ops": [
                        {
                            "op": "set_concept",
                            "id": "dup",
                            "set": {
                                "concept": "c",
                                "meaning": "m",
                                "primary_symbols": ["x"],
                                "related_rule_ids": ["dup"],
                                "related_expectation_ids": [],
                                "related_code_binding_ids": [],
                            },
                        }
                    ]
                }
            )
        self.assertEqual(err.exception.code, "E_INVALID_REQUEST")

    def test_expectation_requires_existing_rules(self) -> None:
        self.engine.set_rule({"id": "r1", "lang": "pyexpr", "rule": "x > 0"})
        self.engine.set_rule({"id": "r2", "lang": "pyexpr", "rule": "x >= 0"})
        self.assertEqual(
            self.engine.set_expectation({"id": "e1", "kind": "entails", "a_ref": "r1", "b_ref": "r2"}),
            {"ok": True},
        )

        with self.assertRaises(LogicError) as err:
            self.engine.set_expectation({"id": "e2", "kind": "entails", "a_ref": "r1", "b_ref": "missing"})
        self.assertEqual(err.exception.code, "E_INVALID_REQUEST")

    def test_context_patch_constraints_and_transitive_anchor(self) -> None:
        self.engine.set_rule({"id": "r1", "lang": "pyexpr", "rule": "x > 0"})

        with self.assertRaises(LogicError):
            self.engine.context_patch(
                {
                    "ops": [
                        {
                            "op": "set_concept",
                            "id": "c_bad",
                            "set": {
                                "concept": "Orphan",
                                "meaning": "No links",
                                "primary_symbols": ["x"],
                                "related_rule_ids": [],
                                "related_expectation_ids": [],
                                "related_code_binding_ids": [],
                            },
                        }
                    ]
                }
            )

        self.assertEqual(
            self.engine.context_patch(
                {
                    "ops": [
                        {
                            "op": "set_concept",
                            "id": "c1",
                            "set": {
                                "concept": "C1",
                                "meaning": "Anchored concept",
                                "primary_symbols": ["x"],
                                "related_rule_ids": ["r1"],
                                "related_expectation_ids": [],
                                "related_code_binding_ids": [],
                            },
                        },
                        {
                            "op": "set_code_binding",
                            "id": "b1",
                            "set": {
                                "path": "src/main.py",
                                "related_rule_ids": [],
                                "related_expectation_ids": [],
                                "related_concept_ids": ["c1"],
                            },
                        },
                        {
                            "op": "set_concept",
                            "id": "c1",
                            "set": {
                                "related_code_binding_ids": ["b1"],
                            },
                        },
                    ]
                }
            ),
            {"ok": True},
        )

        with self.assertRaises(LogicError):
            self.engine.remove_rule({"id": "r1"})

    def test_logic_check_detail_levels(self) -> None:
        self.engine.set_rule({"id": "r1", "lang": "pyexpr", "rule": "x > 0"})

        minimal = self.engine.check_v5({"hypothesis": {"facts": {"x": 1}}, "detail_level": "minimal"})
        self.assertTrue(minimal["ok"])
        self.assertIn("baseline", minimal["result"])
        self.assertIn("candidate", minimal["result"])
        self.assertIn("breaks", minimal["result"])
        self.assertNotIn("delta", minimal["result"])

        full = self.engine.check_v5({"hypothesis": {"facts": {"x": 1}}, "detail_level": "full"})
        self.assertTrue(full["ok"])
        self.assertIn("influence", full["result"])

    def test_logic_list_id_mode_and_mutual_exclusion(self) -> None:
        self.engine.set_rule({"id": "r1", "lang": "pyexpr", "rule": "x > 0"})

        by_id = self.engine.list_items({"id": "r1"})
        self.assertTrue(by_id["ok"])
        self.assertEqual(len(by_id["result"]["items"]), 1)
        self.assertEqual(by_id["result"]["items"][0]["id"], "r1")

        with self.assertRaises(LogicError):
            self.engine.list_items({"id": "r1", "show": ["rules"]})


if __name__ == "__main__":
    unittest.main()
