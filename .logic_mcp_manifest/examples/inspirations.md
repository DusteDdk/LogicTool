# Logic MCP Inspirations: 24 Demonstration Chains

Each chain follows:
- situation during problem solving
- realization of how Logic MCP helps
- concrete tool calls
- benefit gained

## 1. I discover a hidden assumption while reading code
Situation: While implementing retry logic, I notice the code assumes `retry_limit >= 0` but nothing enforces it.
Realization: I can convert this assumption into a persistent invariant so every future hypothesis is checked against it.
Concrete calls:
```text
logic_set_rule {"id":"r_retry_nonneg","lang":"pyexpr","rule":"retry_limit >= 0"}
logic_check {"hypothesis":{"facts":{"retry_limit":"?r:Int"}},"detail_level":"compact"}
```
Benefit: The assumption becomes explicit and machine-checked instead of remaining in my head.

## 2. I need a witness value, not a guess
Situation: I need to know if constraints are satisfiable, but manual reasoning is messy.
Realization: I can leave facts symbolic and ask for a model.
Concrete calls:
```text
logic_check {
  "hypothesis":{"facts":{"batch_size":"?b:Int","timeout_ms":"?t:Int"}},
  "detail_level":"full"
}
```
Benefit: I get concrete satisfying values quickly, instead of hand-solving.

## 3. I want to test a stricter rule before committing
Situation: I consider tightening timeout limits and want impact first.
Realization: I can patch rules temporarily without touching persistent state.
Concrete calls:
```text
logic_check {
  "hypothesis":{"patch":{"set_rules":{"r_timeout_tmp":{"lang":"pyexpr","rule":"timeout_ms <= 8000"}},"remove_rules":[]}},
  "detail_level":"more"
}
```
Benefit: I evaluate candidate design safely before persisting it.

## 4. I suspect a rule is unnecessary
Situation: I think a rule might be redundant.
Realization: I can remove it in hypothesis and see whether behavior degrades.
Concrete calls:
```text
logic_check {
  "hypothesis":{"patch":{"set_rules":{},"remove_rules":["r_retry_nonneg"]}},
  "detail_level":"more"
}
```
Benefit: I can validate necessity with evidence, not intuition.

## 5. I fear omission bugs
Situation: A rule exists, but I worry other required consequences are not guaranteed.
Realization: Add an expectation to detect missing implications.
Concrete calls:
```text
logic_set_expectation {"id":"e_retry_implies_backoff","kind":"entails","a_ref":"r_retry_policy","b_ref":"r_backoff_nonneg"}
logic_check {"hypothesis":{},"detail_level":"more"}
```
Benefit: Omissions show up as expectation failures with counterexamples.

## 6. I refactor and need semantic parity
Situation: I replaced an old rule expression with a new one.
Realization: Use an `equivalent` expectation to prove parity.
Concrete calls:
```text
logic_set_rule {"id":"r_old","lang":"pyexpr","rule":"a + b == c"}
logic_set_rule {"id":"r_new","lang":"pyexpr","rule":"c - b == a"}
logic_set_expectation {"id":"e_old_new_equiv","kind":"equivalent","a_ref":"r_old","b_ref":"r_new"}
logic_check {"detail_level":"more"}
```
Benefit: Refactor correctness gets checked formally.

## 7. I need SMT declarations reusable across many checks
Situation: Multiple rules need shared symbol declarations.
Realization: Store declarations in a bundle once.
Concrete calls:
```text
logic_set_bundle {"id":"b_symbols","bundle":["(declare-const x Int)","(declare-const y Int)"]}
logic_set_rule {"id":"r_bounds","lang":"pyexpr","rule":"x <= y"}
```
Benefit: Cleaner model structure and less repetition.

## 8. I need a cheap global snapshot before editing
Situation: I join an ongoing task and need orientation.
Realization: Use minimal listing to get IDs and types first.
Concrete calls:
```text
logic_list {"show":["all"],"detail_level":"minimal","limit":200}
```
Benefit: Fast inventory scan with low token cost.

## 9. I need full context on one specific item
Situation: I want exact payload of one rule/binding before changing it.
Realization: Single-ID lookup is cheaper than full dumps.
Concrete calls:
```text
logic_list {"id":"r_bounds"}
logic_list {"id":"cb_pricing"}
```
Benefit: I fetch only what I need in full detail.

## 10. I want traceability from logic to code
Situation: I can prove logic properties, but need to map them to implementation.
Realization: Use concepts + code bindings in one atomic patch.
Concrete calls:
```text
logic_context_patch {
  "ops":[
    {"op":"set_concept","id":"c_pricing_floor","set":{"concept":"Pricing floor","meaning":"Price must not be negative","primary_symbols":["price_cents"],"related_rule_ids":["r_price_nonneg"],"related_expectation_ids":[],"related_code_binding_ids":["cb_price_fn"]}},
    {"op":"set_code_binding","id":"cb_price_fn","set":{"path":"src/pricing/calc.py","related_rule_ids":["r_price_nonneg"],"related_expectation_ids":[],"related_concept_ids":["c_pricing_floor"]}}
  ]
}
```
Benefit: Logical guarantees become discoverable in code navigation.

## 11. I want ownership metadata for audits
Situation: Rules exist but no one knows who owns them.
Realization: Attach metadata to rule and expectation.
Concrete calls:
```text
logic_context_patch {"ops":[
  {"op":"set_rule_meta","id":"r_price_nonneg","set":{"owner":"pricing-team","stability":"strict"}},
  {"op":"set_expectation_meta","id":"e_price_consistency","set":{"purpose":"regression guard"}}
]}
```
Benefit: Better maintenance and review accountability.

## 12. I need pagination for large inventories
Situation: There are many items and one response gets too large.
Realization: Paginate with `limit` and `cursor`.
Concrete calls:
```text
logic_list {"show":["all"],"detail_level":"minimal","limit":50}
logic_list {"show":["all"],"detail_level":"minimal","limit":50,"cursor":"50"}
```
Benefit: Predictable payload sizes and controllable context window usage.

## 13. I need proof that requirements are contradictory
Situation: Two stakeholders gave conflicting requirements.
Realization: Encode both and ask solver.
Concrete calls:
```text
logic_set_rule {"id":"r_a","lang":"pyexpr","rule":"x > 10"}
logic_set_rule {"id":"r_b","lang":"pyexpr","rule":"x < 5"}
logic_check {"detail_level":"more"}
```
Benefit: Conflict is surfaced as `unsat` with diagnostic structure.

## 14. I need to measure change impact quickly
Situation: I edited one policy and want to know what it breaks.
Realization: Candidate vs baseline gives direct impact.
Concrete calls:
```text
logic_check {
  "hypothesis":{"patch":{"set_rules":{"r_policy_tmp":{"lang":"pyexpr","rule":"limit <= 3"}},"remove_rules":[]}},
  "detail_level":"compact"
}
```
Benefit: `breaks` and `delta` immediately summarize impact.

## 15. I want to verify that defaults are not over-constraining
Situation: I suspect my current facts accidentally make everything pass.
Realization: Run symbolic facts and compare to concrete facts.
Concrete calls:
```text
logic_check {"hypothesis":{"facts":{"x":"?x:Int","y":"?y:Int"}},"detail_level":"full"}
logic_check {"hypothesis":{"facts":{"x":2,"y":2}},"detail_level":"compact"}
```
Benefit: I distinguish true invariants from artifact of fixed test values.

## 16. I need safe cleanup order
Situation: I want to remove a rule but it may be referenced.
Realization: Attempt removal and follow dependency errors.
Concrete calls:
```text
logic_remove_rule {"id":"r_price_nonneg"}
logic_list {"show":["expectations","concepts","code_bindings"],"detail_level":"more"}
logic_remove_expectation {"id":"e_price_consistency"}
logic_remove_rule {"id":"r_price_nonneg"}
```
Benefit: Avoids accidental graph corruption and documents dependency flow.

## 17. I need to keep the model token-light during active coding
Situation: I am in rapid edit loop and cannot afford verbose responses every call.
Realization: Use minimal list and minimal check most of the time, escalate only when needed.
Concrete calls:
```text
logic_list {"show":["rules"],"detail_level":"minimal"}
logic_check {"hypothesis":{"facts":{"x":1}},"detail_level":"minimal"}
logic_check {"hypothesis":{"facts":{"x":1}},"detail_level":"full"}
```
Benefit: Spend tokens only when deep diagnostics are needed.

## 18. I need an implementation map for onboarding another agent
Situation: A second agent joins and needs shared understanding.
Realization: Build concept and code-binding graph tied to rules.
Concrete calls:
```text
logic_context_patch {"ops":[
  {"op":"set_concept","id":"c_auth_window","set":{"concept":"Auth validity window","meaning":"Token age must stay under threshold","primary_symbols":["token_age_ms","max_age_ms"],"related_rule_ids":["r_token_age_cap"],"related_expectation_ids":["e_auth_safety"],"related_code_binding_ids":["cb_auth_guard"]}},
  {"op":"set_code_binding","id":"cb_auth_guard","set":{"path":"src/auth/guard.ts","kind":"source","function_or_behavior":"validateTokenAge","related_rule_ids":["r_token_age_cap"],"related_expectation_ids":["e_auth_safety"],"related_concept_ids":["c_auth_window"]}}
]}
logic_list {"show":["concepts","code_bindings"],"detail_level":"full"}
```
Benefit: New agents get an explicit logic-to-code mental model.

## 19. I need to confirm a bug report is actually possible
Situation: A bug report says a forbidden state occurred.
Realization: Encode the alleged state as facts and check satisfiability.
Concrete calls:
```text
logic_check {"hypothesis":{"facts":{"balance_cents":-1,"credit_limit_cents":100}},"detail_level":"more"}
```
Benefit: Quickly separates real bug possibility from impossible report.

## 20. I need to find exactly which rules are causing unsat
Situation: Candidate is unsat but root cause is unclear.
Realization: Use `detail_level:"more"` or `full` for unsat core details.
Concrete calls:
```text
logic_check {
  "hypothesis":{"patch":{"set_rules":{"r_tmp":{"lang":"pyexpr","rule":"x < 0"}},"remove_rules":[]}},
  "detail_level":"more"
}
```
Benefit: Unsat core narrows debugging to specific rule IDs.

## 21. I need to codify a requirement document progressively
Situation: Spec is written in prose and evolves over time.
Realization: Add each discovered requirement as rule or expectation, then annotate concept source.
Concrete calls:
```text
logic_set_rule {"id":"r_rate_limit_cap","lang":"pyexpr","rule":"requests_per_minute <= 120"}
logic_set_expectation {"id":"e_rate_cap_consistency","kind":"entails","a_ref":"r_rate_limit_cap","b_ref":"r_nonnegative_requests"}
logic_context_patch {"ops":[{"op":"set_concept","id":"c_rate_limit","set":{"concept":"Rate limit","meaning":"Per-minute request cap","primary_symbols":["requests_per_minute"],"source_ref":"spec/rate-limits.md#core-rule","related_rule_ids":["r_rate_limit_cap"],"related_expectation_ids":["e_rate_cap_consistency"],"related_code_binding_ids":[]}}]}
```
Benefit: Prose requirements become executable constraints.

## 22. I need to test two competing designs quickly
Situation: I have Design A and Design B formulas.
Realization: Compare each using temporary patch rules with same baseline.
Concrete calls:
```text
logic_check {"hypothesis":{"patch":{"set_rules":{"r_design_tmp":{"lang":"pyexpr","rule":"latency_ms <= 40"}},"remove_rules":[]}},"detail_level":"compact"}
logic_check {"hypothesis":{"patch":{"set_rules":{"r_design_tmp":{"lang":"pyexpr","rule":"latency_ms <= 60"}},"remove_rules":[]}},"detail_level":"compact"}
```
Benefit: Side-by-side feasibility and breakage comparison with no persistent churn.

## 23. I need to verify list filtering by type for focused workflows
Situation: I only care about expectations during review.
Realization: Query only that type at high detail.
Concrete calls:
```text
logic_list {"show":["expectations"],"detail_level":"full"}
```
Benefit: Focused review surface with less noise.

## 24. I need to decommission stale context safely
Situation: A module was removed and bindings are stale.
Realization: Remove code binding and concepts atomically, then verify remaining inventory.
Concrete calls:
```text
logic_context_patch {"ops":[
  {"op":"remove_code_binding","id":"cb_old_module"},
  {"op":"remove_concept","id":"c_old_module"}
]}
logic_list {"show":["concepts","code_bindings","rules"],"detail_level":"compact"}
```
Benefit: Clean context graph without partial updates.

## 25. I need a full rule -> expectation -> concept -> code anchor chain for checkout totals
Situation: I am implementing checkout totals and want machine-checked invariants plus traceability to code.
Realization: I can build rules first, then expectation links, then anchor the implementation site with a code binding.
Concrete calls:
```text
logic_set_bundle {"id":"b_checkout_symbols","bundle":["(declare-const subtotal_cents Int)","(declare-const tax_cents Int)","(declare-const total_cents Int)"]}
logic_set_rule {"id":"r_subtotal_nonneg","lang":"pyexpr","rule":"subtotal_cents >= 0"}
logic_set_rule {"id":"r_tax_nonneg","lang":"pyexpr","rule":"tax_cents >= 0"}
logic_set_rule {"id":"r_total_formula","lang":"pyexpr","rule":"total_cents == subtotal_cents + tax_cents"}
logic_set_rule {"id":"r_total_formula_alt","lang":"pyexpr","rule":"subtotal_cents == total_cents - tax_cents"}
logic_set_expectation {"id":"e_total_formula_equiv","kind":"equivalent","a_ref":"r_total_formula","b_ref":"r_total_formula_alt"}
logic_context_patch {"ops":[
  {"op":"set_concept","id":"c_checkout_total","set":{"concept":"Checkout total","meaning":"Total equals subtotal plus tax","primary_symbols":["subtotal_cents","tax_cents","total_cents"],"related_rule_ids":["r_subtotal_nonneg","r_tax_nonneg","r_total_formula"],"related_expectation_ids":["e_total_formula_equiv"],"related_code_binding_ids":["cb_checkout_total_calc"]}},
  {"op":"set_code_binding","id":"cb_checkout_total_calc","set":{"path":"src/checkout/total.py","kind":"source","function_or_behavior":"calculate_total","symbols_used":["subtotal_cents","tax_cents","total_cents"],"related_rule_ids":["r_total_formula"],"related_expectation_ids":["e_total_formula_equiv"],"related_concept_ids":["c_checkout_total"],"anchor":{"line":38,"text":"def calculate_total"}}},
  {"op":"set_rule_meta","id":"r_total_formula","set":{"owner":"payments","category":"money-invariant"}},
  {"op":"set_expectation_meta","id":"e_total_formula_equiv","set":{"purpose":"formula refactor safety"}}
]}
logic_check {"hypothesis":{"facts":{"subtotal_cents":"?s:Int","tax_cents":"?t:Int","total_cents":"?tot:Int"}},"detail_level":"full"}
```
Benefit: I get correctness checks, semantic-equivalence guardrails, and direct code anchors in one connected graph.

## 26. I need to tie a security invariant to both source and spec document
Situation: A security rule exists in code and policy docs, and I want both linked to the same logic object.
Realization: I can keep one concept with two bindings (`source` and `document`) tied to the same rules/expectations.
Concrete calls:
```text
logic_set_bundle {"id":"b_auth_symbols","bundle":["(declare-const token_age_ms Int)","(declare-const max_age_ms Int)"]}
logic_set_rule {"id":"r_token_age_cap","lang":"pyexpr","rule":"token_age_ms <= max_age_ms"}
logic_set_rule {"id":"r_max_age_nonneg","lang":"pyexpr","rule":"max_age_ms >= 0"}
logic_set_rule {"id":"r_token_age_nonneg","lang":"pyexpr","rule":"token_age_ms >= 0"}
logic_set_expectation {"id":"e_cap_implies_nonneg_bound","kind":"entails","a_ref":"r_token_age_cap","b_ref":"r_token_age_nonneg"}
logic_context_patch {"ops":[
  {"op":"set_concept","id":"c_auth_window","set":{"concept":"Auth validity window","meaning":"Token age stays inside configured validity bounds","primary_symbols":["token_age_ms","max_age_ms"],"source_ref":"security/auth.md#token-window","related_rule_ids":["r_token_age_cap","r_max_age_nonneg","r_token_age_nonneg"],"related_expectation_ids":["e_cap_implies_nonneg_bound"],"related_code_binding_ids":["cb_auth_guard_src","cb_auth_guard_doc"]}},
  {"op":"set_code_binding","id":"cb_auth_guard_src","set":{"path":"src/auth/guard.ts","kind":"source","function_or_behavior":"validateTokenAge","related_rule_ids":["r_token_age_cap"],"related_expectation_ids":["e_cap_implies_nonneg_bound"],"related_concept_ids":["c_auth_window"],"anchor":{"line":74,"text":"if (tokenAgeMs > maxAgeMs) throw"}}},
  {"op":"set_code_binding","id":"cb_auth_guard_doc","set":{"path":"docs/security/auth-window.md","kind":"document","function_or_behavior":"Policy section: Token Validity","related_rule_ids":["r_token_age_cap"],"related_expectation_ids":["e_cap_implies_nonneg_bound"],"related_concept_ids":["c_auth_window"],"anchor":{"line":12,"text":"Token age MUST NOT exceed configured max age"}}}
]}
```
Benefit: One coherent logic concept now points to both executable and normative sources.

## 27. I need to anchor invariants to multiple functions that jointly enforce them
Situation: No single function enforces an invariant; validation is split across layers.
Realization: I can relate one rule to multiple code bindings and still keep one concept.
Concrete calls:
```text
logic_set_rule {"id":"r_refund_nonneg","lang":"pyexpr","rule":"refund_cents >= 0"}
logic_set_rule {"id":"r_refund_cap","lang":"pyexpr","rule":"refund_cents <= order_total_cents"}
logic_set_expectation {"id":"e_refund_cap_consistency","kind":"entails","a_ref":"r_refund_cap","b_ref":"r_refund_nonneg"}
logic_context_patch {"ops":[
  {"op":"set_concept","id":"c_refund_safety","set":{"concept":"Refund safety","meaning":"Refund amount must remain valid and bounded","primary_symbols":["refund_cents","order_total_cents"],"related_rule_ids":["r_refund_nonneg","r_refund_cap"],"related_expectation_ids":["e_refund_cap_consistency"],"related_code_binding_ids":["cb_refund_validator","cb_refund_service"]}},
  {"op":"set_code_binding","id":"cb_refund_validator","set":{"path":"src/billing/refund_validator.py","related_rule_ids":["r_refund_nonneg"],"related_expectation_ids":["e_refund_cap_consistency"],"related_concept_ids":["c_refund_safety"],"anchor":{"line":21,"text":"assert refund_cents >= 0"}}},
  {"op":"set_code_binding","id":"cb_refund_service","set":{"path":"src/billing/refund_service.py","related_rule_ids":["r_refund_cap"],"related_expectation_ids":["e_refund_cap_consistency"],"related_concept_ids":["c_refund_safety"],"anchor":{"line":88,"text":"refund_cents = min(refund_cents, order_total_cents)"}}}
]}
```
Benefit: Split enforcement is represented explicitly instead of being lost across files.

## 28. I need to prove a migration did not change behavior
Situation: I moved logic from Python service to SQL layer and need confidence it stayed equivalent.
Realization: Encode old/new rules, set an equivalence expectation, and bind each side to its source file.
Concrete calls:
```text
logic_set_rule {"id":"r_old_discount","lang":"pyexpr","rule":"discount_cents == min(base_discount_cents, subtotal_cents / 10)"}
logic_set_rule {"id":"r_new_discount","lang":"pyexpr","rule":"discount_cents <= subtotal_cents / 10 and discount_cents <= base_discount_cents"}
logic_set_expectation {"id":"e_discount_migration_equiv","kind":"equivalent","a_ref":"r_old_discount","b_ref":"r_new_discount"}
logic_context_patch {"ops":[
  {"op":"set_concept","id":"c_discount_logic","set":{"concept":"Discount derivation","meaning":"Discount computation across migration boundary","primary_symbols":["discount_cents","subtotal_cents","base_discount_cents"],"related_rule_ids":["r_old_discount","r_new_discount"],"related_expectation_ids":["e_discount_migration_equiv"],"related_code_binding_ids":["cb_discount_old_py","cb_discount_new_sql"]}},
  {"op":"set_code_binding","id":"cb_discount_old_py","set":{"path":"src/pricing/discount.py","related_rule_ids":["r_old_discount"],"related_expectation_ids":["e_discount_migration_equiv"],"related_concept_ids":["c_discount_logic"],"anchor":{"line":44,"text":"def compute_discount"}}},
  {"op":"set_code_binding","id":"cb_discount_new_sql","set":{"path":"migrations/2026_02_discount.sql","kind":"document","related_rule_ids":["r_new_discount"],"related_expectation_ids":["e_discount_migration_equiv"],"related_concept_ids":["c_discount_logic"],"anchor":{"line":63,"text":"CASE WHEN"}}}
]}
logic_check {"detail_level":"more"}
```
Benefit: Migration safety is checked formally and mapped to both old and new artifacts.

## 29. I need to test if an anchor rule can be relaxed without violating downstream expectations
Situation: I am tempted to weaken a core invariant for performance reasons.
Realization: Temporarily patch the anchor rule and observe expectation failures before making permanent changes.
Concrete calls:
```text
logic_check {
  "hypothesis":{"patch":{"set_rules":{"r_refund_cap":{"lang":"pyexpr","rule":"refund_cents <= order_total_cents + 500"}},"remove_rules":[]}},
  "detail_level":"more"
}
logic_list {"show":["expectations","concepts","code_bindings"],"detail_level":"more"}
```
Benefit: I can see exactly which conceptual and code-linked guarantees become risky.

## 30. I need a graph-focused review handoff
Situation: I am handing work to another agent and want them to reason from relationships, not scattered notes.
Realization: I can produce full graph outputs for only the relevant IDs.
Concrete calls:
```text
logic_list {"show":["rules","expectations"],"detail_level":"full"}
logic_list {"show":["concepts","code_bindings"],"detail_level":"full"}
logic_list {"id":"c_checkout_total"}
logic_list {"id":"cb_checkout_total_calc"}
```
Benefit: Handoff context is precise, auditable, and immediately navigable.

## 31. I need to pin a fast-fail invariant directly to a guard clause
Situation: A critical runtime guard must never drift from the formal invariant.
Realization: Anchor the invariant rule to the exact guard line and keep an expectation over related rules.
Concrete calls:
```text
logic_set_rule {"id":"r_quantity_nonneg","lang":"pyexpr","rule":"quantity >= 0"}
logic_set_rule {"id":"r_can_ship","lang":"pyexpr","rule":"quantity <= inventory_count"}
logic_set_expectation {"id":"e_ship_guard_sound","kind":"entails","a_ref":"r_can_ship","b_ref":"r_quantity_nonneg"}
logic_context_patch {"ops":[
  {"op":"set_concept","id":"c_shipping_guard","set":{"concept":"Shipping guard","meaning":"Only valid quantities can be shipped","primary_symbols":["quantity","inventory_count"],"related_rule_ids":["r_quantity_nonneg","r_can_ship"],"related_expectation_ids":["e_ship_guard_sound"],"related_code_binding_ids":["cb_shipping_guard"]}},
  {"op":"set_code_binding","id":"cb_shipping_guard","set":{"path":"src/shipping/validate.py","related_rule_ids":["r_quantity_nonneg","r_can_ship"],"related_expectation_ids":["e_ship_guard_sound"],"related_concept_ids":["c_shipping_guard"],"anchor":{"line":17,"text":"if quantity < 0: raise"}}}
]}
```
Benefit: The guard line is now linked to formal correctness constraints.

## 32. I need to prune and rebuild a relationship chain safely
Situation: Concept structure changed and old links are stale.
Realization: Remove old bindings/concepts, then rebuild with new rule and expectation links in one controlled sequence.
Concrete calls:
```text
logic_context_patch {"ops":[{"op":"remove_code_binding","id":"cb_old_guard"},{"op":"remove_concept","id":"c_old_guard"}]}
logic_set_rule {"id":"r_new_guard","lang":"pyexpr","rule":"threshold_ms >= 0 and value_ms <= threshold_ms"}
logic_set_expectation {"id":"e_new_guard_sound","kind":"entails","a_ref":"r_new_guard","b_ref":"r_threshold_nonneg"}
logic_context_patch {"ops":[
  {"op":"set_concept","id":"c_new_guard","set":{"concept":"New guard model","meaning":"Updated bound checks","primary_symbols":["value_ms","threshold_ms"],"related_rule_ids":["r_new_guard","r_threshold_nonneg"],"related_expectation_ids":["e_new_guard_sound"],"related_code_binding_ids":["cb_new_guard"]}},
  {"op":"set_code_binding","id":"cb_new_guard","set":{"path":"src/runtime/guard.py","related_rule_ids":["r_new_guard"],"related_expectation_ids":["e_new_guard_sound"],"related_concept_ids":["c_new_guard"],"anchor":{"line":53,"text":"if value_ms > threshold_ms"}}}
]}
logic_check {"hypothesis":{"facts":{"value_ms":"?v:Int","threshold_ms":"?t:Int"}},"detail_level":"full"}
```
Benefit: Relationship graph stays coherent while evolving to the new design.
