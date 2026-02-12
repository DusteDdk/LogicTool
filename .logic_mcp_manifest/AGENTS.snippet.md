## Logic MCP
Use Logic MCP by default for constraint reasoning and what-if checks.
Trigger immediately when you identify constants, invariants, bounds, or logical dependencies.
Use it for thought experiments to test relaxations and discover less-constrained valid designs.
Use it to set expectations or rules when you identify that some detail or relationship may be fragile or low locality (risk regression when working on other, seemingly innocent or unrelated thing).
Use it to check that unit tests are reflecting actual expectations.
Use it to get an overview of concepts, rules, expectations and source code locations of particular interest (and the relationship between all of these).

1. Read `.logic_mcp_manifest/manifest.md`.
2. Build/update state incrementally with `logic_set_*` / `logic_remove_*` tools.
3. Use `logic_context_patch` to keep concepts and code bindings aligned to logic ids.
4. Use `.logic_mcp_manifest/examples.md` to construct checks and thought experiments.
5. Before final compatibility/safety claims, run `logic_check`.
6. When constraints are expressible in supported logic, prefer solver-backed results over reasoning-only conclusions.
7. Prefer short payloads and temporary `hypothesis.patch` overlays for experiments.

 ## Look Before Act (Required)
  - Before adding or modifying any Logic MCP rules/bundles/expectations, run `logic_list`.
  - Summarize existing relevant rules/bundles in the reply before proposing changes.
  - If a new rule overlaps existing ones, call it out explicitly.
  - Only add/replace/delete after the summary.

  If you want this enforced for all edits (not just Logic MCP), add:

  ## Pre-Edit Scan (Encouraged)
  - Before editing any file, open it and summarize the relevant section(s).
  - For code changes, run `rg` to locate relevant references and mention what you found.

  ## If you wonder if there may be more uses for the Logic MCP tool
  - Read `.logic_mcp_manifest/use-case-examples.md`


