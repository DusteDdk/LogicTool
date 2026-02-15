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
        self.log_file = self.session_dir / "log.jsonl"
        shutil.rmtree(self.session_dir, ignore_errors=True)
        if self.log_file.exists():
            self.log_file.unlink()
        self.engine = LogicEngine(self.session_id)

    def tearDown(self) -> None:
        shutil.rmtree(self.session_dir, ignore_errors=True)
        if self.log_file.exists():
            self.log_file.unlink()

    def _motivation(self, rationale: str, motivated_by: str | None = None) -> dict:
        out = {"rationale": rationale}
        if motivated_by is not None:
            out["motivatedByItem"] = motivated_by
        return out

    def test_set_rule_and_unified_list_flow(self) -> None:
        out = self.engine.list_items({})
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"], {"items": []})

        self.assertEqual(
            self.engine.set_rule(
                {
                    "id": "r1",
                    "lang": "pyexpr",
                    "rule": "x > 0",
                    "intent": "Ensure x is positive",
                    "motivation": self._motivation("Capture positive invariant"),
                }
            ),
            {"ok": True},
        )

        listed = self.engine.list_items({"show": ["rules"], "detail_level": "more"})
        self.assertTrue(listed["ok"])
        self.assertEqual(len(listed["result"]["items"]), 1)
        item = listed["result"]["items"][0]
        self.assertEqual(item["id"], "r1")
        self.assertEqual(item["type"], "rule")
        self.assertIn("summary", item)
        self.assertIn("intent", item)
        self.assertNotIn("content", item)

        read_item = self.engine.read_item({"id": "r1"})
        self.assertTrue(read_item["ok"])
        self.assertEqual(read_item["result"]["item"]["id"], "r1")
        self.assertIn("content", read_item["result"]["item"])
        self.assertIn("motivation", read_item["result"]["item"])

        self.assertEqual(self.engine.remove_rule({"id": "r1"}), {"ok": True})
        listed_after = self.engine.list_items({"show": ["rules"]})
        self.assertEqual(listed_after["result"], {"items": []})

    def test_global_id_uniqueness_across_types(self) -> None:
        self.engine.set_rule(
            {
                "id": "dup",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Rule intent",
                "motivation": self._motivation("Create rule"),
            }
        )
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
                                "motivation": self._motivation("Explain rule"),
                            },
                        }
                    ]
                }
            )
        self.assertEqual(err.exception.code, "E_INVALID_REQUEST")

    def test_expectation_requires_existing_rules(self) -> None:
        self.engine.set_rule(
            {
                "id": "r1",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Positive x",
                "motivation": self._motivation("Create r1"),
            }
        )
        self.engine.set_rule(
            {
                "id": "r2",
                "lang": "pyexpr",
                "rule": "x >= 0",
                "intent": "Non-negative x",
                "motivation": self._motivation("Create r2"),
            }
        )
        self.assertEqual(
            self.engine.set_expectation(
                {
                    "id": "e1",
                    "kind": "entails",
                    "a_ref": "r1",
                    "b_ref": "r2",
                    "motivation": self._motivation("Guard entailment"),
                }
            ),
            {"ok": True},
        )

        with self.assertRaises(LogicError) as err:
            self.engine.set_expectation(
                {
                    "id": "e2",
                    "kind": "entails",
                    "a_ref": "r1",
                    "b_ref": "missing",
                    "motivation": self._motivation("Invalid expectation"),
                }
            )
        self.assertEqual(err.exception.code, "E_INVALID_REQUEST")

    def test_context_patch_constraints_and_transitive_anchor(self) -> None:
        self.engine.set_rule(
            {
                "id": "r1",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Positive x",
                "motivation": self._motivation("Create rule"),
            }
        )

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
                                "motivation": self._motivation("Bad concept"),
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
                                "motivation": self._motivation("Create concept"),
                            },
                        },
                        {
                            "op": "set_code_binding",
                            "id": "b1",
                            "set": {
                                "path": "src/main.py",
                                "from": {"line": 10},
                                "to": {"line": 20},
                                "related_rule_ids": [],
                                "related_expectation_ids": [],
                                "related_concept_ids": ["c1"],
                                "motivation": self._motivation("Bind concept to source"),
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

        concept_full = self.engine.read_item({"id": "c1", "detail_level": "full"})
        self.assertTrue(concept_full["ok"])
        concept_item = concept_full["result"]["item"]
        self.assertEqual(concept_item["id"], "c1")
        self.assertEqual(concept_item["version"], 2)
        self.assertIn("motivation", concept_item)

        binding_full = self.engine.read_item({"id": "b1", "detail_level": "full"})
        self.assertTrue(binding_full["ok"])
        binding_item = binding_full["result"]["item"]
        self.assertEqual(binding_item["id"], "b1")
        self.assertNotIn("version", binding_item)
        self.assertNotIn("created_at", binding_item)
        self.assertIn("from", binding_item)
        self.assertIn("to", binding_item)

        with self.assertRaises(LogicError):
            self.engine.remove_rule({"id": "r1"})

    def test_context_patch_warns_when_to_without_from(self) -> None:
        self.engine.set_rule(
            {
                "id": "r1",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Positive x",
                "motivation": self._motivation("Create rule"),
            }
        )

        out = self.engine.context_patch(
            {
                "ops": [
                    {
                        "op": "set_code_binding",
                        "id": "b_warn",
                        "set": {
                            "path": "src/main.py",
                            "to": {"line": 20},
                            "related_rule_ids": ["r1"],
                            "related_expectation_ids": [],
                            "related_concept_ids": [],
                            "motivation": self._motivation("Binding with malformed range"),
                        },
                    }
                ]
            }
        )
        self.assertTrue(out["ok"])
        self.assertIn("warnings", out)
        self.assertEqual(out["warnings"][0].get("code"), "W_BINDING_TO_WITHOUT_FROM")

        by_id = self.engine.read_item({"id": "b_warn", "detail_level": "full"})
        self.assertTrue(by_id["ok"])
        item = by_id["result"]["item"]
        self.assertNotIn("to", item)

    def test_path_disallows_line_range_encoding(self) -> None:
        self.engine.set_rule(
            {
                "id": "r1",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Positive x",
                "motivation": self._motivation("Create rule"),
            }
        )
        with self.assertRaises(LogicError) as err:
            self.engine.context_patch(
                {
                    "ops": [
                        {
                            "op": "set_code_binding",
                            "id": "b_bad_path",
                            "set": {
                                "path": "src/main.py:10-20",
                                "related_rule_ids": ["r1"],
                                "related_expectation_ids": [],
                                "related_concept_ids": [],
                                "motivation": self._motivation("Bad encoded path"),
                            },
                        }
                    ]
                }
            )
        self.assertEqual(err.exception.code, "E_INVALID_REQUEST")

    def test_read_includes_stale_motivation_note(self) -> None:
        self.engine.set_bundle(
            {
                "id": "b1",
                "bundle": ["(declare-const x Int)"],
                "motivation": self._motivation("Declare shared symbols"),
            }
        )
        self.engine.set_rule(
            {
                "id": "r1",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Positive x",
                "motivation": self._motivation("Rule from bundle", motivated_by="b1"),
            }
        )
        self.engine.remove_bundle({"id": "b1"})

        by_id = self.engine.read_item({"id": "r1", "detail_level": "full"})
        self.assertTrue(by_id["ok"])
        motivation = by_id["result"]["item"].get("motivation", {})
        self.assertEqual(motivation.get("motivatedByItem"), "b1")
        self.assertIn("note", motivation)

    def test_logic_check_detail_levels(self) -> None:
        self.engine.set_rule(
            {
                "id": "r1",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Positive x",
                "motivation": self._motivation("Create rule"),
            }
        )

        minimal = self.engine.check_v5({"hypothesis": {"facts": {"x": 1}}, "detail_level": "minimal"})
        self.assertTrue(minimal["ok"])
        self.assertIn("baseline", minimal["result"])
        self.assertIn("candidate", minimal["result"])
        self.assertIn("breaks", minimal["result"])
        self.assertNotIn("delta", minimal["result"])

        full = self.engine.check_v5({"hypothesis": {"facts": {"x": 1}}, "detail_level": "full"})
        self.assertTrue(full["ok"])
        self.assertIn("influence", full["result"])

    def test_logic_check_numeric_fact_type_behavior(self) -> None:
        self.engine.set_rule(
            {
                "id": "r_num",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Positive x",
                "motivation": self._motivation("Create numeric rule"),
            }
        )

        integer_ok = self.engine.check_v5({"hypothesis": {"facts": {"x": 1}}, "detail_level": "compact"})
        self.assertTrue(integer_ok["ok"])
        self.assertEqual(integer_ok["result"]["baseline"]["status"], "sat")

        integral_float_ok = self.engine.check_v5({"hypothesis": {"facts": {"x": 1.0}}, "detail_level": "compact"})
        self.assertTrue(integral_float_ok["ok"])
        self.assertEqual(integral_float_ok["result"]["baseline"]["status"], "sat")

        with self.assertRaises(LogicError) as err:
            self.engine.check_v5({"hypothesis": {"facts": {"x": 2.5}}, "detail_level": "compact"})
        self.assertEqual(err.exception.code, "E_INVALID_REQUEST")
        self.assertIn("expected Int, got Real", err.exception.message)

    def test_logic_read_and_list_constraints(self) -> None:
        self.engine.set_rule(
            {
                "id": "r1",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Positive x",
                "motivation": self._motivation("Create rule"),
            }
        )

        by_id = self.engine.read_item({"id": "r1", "detail_level": "full"})
        self.assertTrue(by_id["ok"])
        self.assertEqual(by_id["result"]["item"]["id"], "r1")
        self.assertIn("content", by_id["result"]["item"])

        with self.assertRaises(LogicError):
            self.engine.list_items({"id": "r1"})

        with self.assertRaises(LogicError):
            self.engine.list_items({"detail_level": "full"})

    def test_logic_check_compact_returns_model_and_metrics_by_default(self) -> None:
        self.engine.set_rule(
            {
                "id": "r1",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Positive x",
                "motivation": self._motivation("Create rule"),
            }
        )
        out = self.engine.check_v5({"hypothesis": {"facts": {"x": 1}}, "detail_level": "compact"})
        self.assertTrue(out["ok"])
        self.assertIn("model", out["result"]["baseline"])
        self.assertEqual(out["result"]["baseline"]["model"]["x"], 1)
        self.assertIn("metrics", out["result"])
        self.assertIn("baseline_ms", out["result"]["metrics"])
        self.assertIn("candidate_ms", out["result"]["metrics"])

    def test_logic_check_model_scope_selected(self) -> None:
        self.engine.set_rule(
            {
                "id": "r_xy",
                "lang": "pyexpr",
                "rule": "x > 0 and y > 0",
                "intent": "Positive variables",
                "motivation": self._motivation("Create rule"),
            }
        )
        out = self.engine.check_v5(
            {
                "hypothesis": {"facts": {"x": 1, "y": 2}},
                "detail_level": "compact",
                "return_model": True,
                "model_scope": "selected",
                "model_symbols": ["y"],
            }
        )
        self.assertTrue(out["ok"])
        model = out["result"]["baseline"]["model"]
        self.assertNotIn("x", model)
        self.assertEqual(model["y"], 2)

    def test_logic_check_assumptions_enable_parametric_trials(self) -> None:
        self.engine.set_rule(
            {
                "id": "r1",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Positive x",
                "motivation": self._motivation("Create rule"),
            }
        )
        out = self.engine.check_v5(
            {
                "hypothesis": {
                    "facts": {"x": 1},
                    "assumptions": [{"id": "a_neg", "lang": "pyexpr", "rule": "x < 0"}],
                },
                "detail_level": "more",
            }
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"]["baseline"]["status"], "unsat")
        self.assertIn("a_neg", out["result"]["baseline"].get("unsat_core", []))

    def test_smt2_accepts_set_logic_and_set_option(self) -> None:
        out = self.engine.set_bundle(
            {
                "id": "b_smt",
                "bundle": [
                    "(set-logic QF_LIA)",
                    "(set-option :produce-models true)",
                    "(declare-const x Int)",
                    "(assert (> x 0))",
                ],
                "motivation": self._motivation("SMT options"),
            }
        )
        self.assertEqual(out, {"ok": True})

    def test_pyexpr_supports_implies_and_xor(self) -> None:
        self.engine.set_rule(
            {
                "id": "r_bool",
                "lang": "pyexpr",
                "rule": "implies(a, b) and xor(a, b)",
                "intent": "Boolean helpers",
                "motivation": self._motivation("Use helper functions"),
            }
        )
        sat_out = self.engine.check_v5(
            {"hypothesis": {"facts": {"a": False, "b": True}}, "detail_level": "compact", "return_model": False}
        )
        self.assertEqual(sat_out["result"]["baseline"]["status"], "sat")

    def test_logic_reset_clears_store_and_logs(self) -> None:
        self.engine.set_rule(
            {
                "id": "r1",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Positive x",
                "motivation": self._motivation("Create rule"),
            }
        )
        self.log_file.write_text("{}", encoding="utf-8")
        out = self.engine.reset_session({"confirm": "reset-session", "wipe_logs": True})
        self.assertTrue(out["ok"])
        self.assertTrue(out["result"]["wiped_inventory"])
        self.assertFalse(self.log_file.exists())
        listed = self.engine.list_items({})
        self.assertEqual(listed["result"]["items"], [])


if __name__ == "__main__":
    unittest.main()
