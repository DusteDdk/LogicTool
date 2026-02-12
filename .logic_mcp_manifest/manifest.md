# Logic MCP Manifest

Use this MCP server for fast, persistent logic checks.
When constraints are expressible in `pyexpr`/`smt2`, this gives more consistent answers than reasoning alone.

Trigger:
- During spec clarification, if you discover constants, invariants, bounds, or logical dependencies.
- Before final claims about compatibility, safety, or edge-case behavior.
- During thought experiments to find simpler/less-constrained valid solutions.

Primary tool use:
- `logic_list`: unified listing for bundles, rules, expectations, concepts, and code bindings.
- `logic_set_bundle` / `logic_remove_bundle`: maintain persistent SMT2 bundles.
- `logic_set_rule` / `logic_remove_rule`: maintain persistent rules (`pyexpr` or `smt2`).
- `logic_set_expectation` / `logic_remove_expectation`: maintain expectation links (`entails` / `equivalent`).
- `logic_context_patch`: atomically maintain concepts, code bindings, and metadata links.
- `logic_check`: run baseline vs candidate what-if with temporary `facts` and rule patch overlays.

Quick workflow:
1. Add declarations (`logic_set_bundle`).
2. Add invariants/dependencies as soon as they are identified (`logic_set_rule`).
3. Add expectations (`logic_set_expectation`) when omission risk exists.
4. Add context links (`logic_context_patch`) when concepts or code references are discovered.
5. Use `logic_check` for risky changes and what-if thought experiments.
6. Inspect `breaks`, `delta`, and (at higher detail levels) `expectations`, `unsat_core`, `influence`.
7. Use `.logic_mcp_manifest/examples.md` for compact request patterns.

Rules:
- Keep payloads minimal.
- Use `hypothesis.patch` for temporary experiments only.
