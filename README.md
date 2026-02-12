# Logic MCP Server

`logic_mcp_server.py` is an MCP streamable HTTP server that provides persistent, namespace-scoped logical constraint checking for agentic what-if analysis.
It runs in HTTP mode only.
Runtime code is split into focused modules:
- `logic_mcp/transport_http.py` (HTTP transport + server lifecycle)
- `logic_mcp/engine.py` (tool schemas, request handlers, solver engine)
- `logic_mcp/store.py` (persistent model store)
- `logic_mcp/audit_log.py` (JSONL audit logging)
- `logic_mcp/errors.py`, `logic_mcp/paths.py` (shared primitives)

It supports:
- Persistent `bundles` (SMT2 fragments), `rules` (`pyexpr` or `smt2`), and `expectations`
- Persistent context inventory (`concepts`, `code_bindings`) via atomic `logic_context_patch`
- What-if checks with temporary hypothesis facts and rule patch overlays (`set_rules`, `remove_rules`)
- Baseline vs candidate evaluation with `sat` / `unsat` / `unknown`
- Unsat cores, witness models, expectation checks, influence analysis, and delta reporting (by `detail_level`)
- Strict SMT2 command acceptance (`declare-*`, `define-fun*`, `assert`)

Persistent state is stored in `logic_store/<sessionId>/session.json`.
Session id is derived from the URL path segment in `/sessions/<sessionId>/` (and never from tool payload fields).
Tool-call audit logs are written per session to `logic_store/<sessionId>_log.jsonl`.
Each line is a JSON object with shape:
`{"time":"<timestamp>","call":<raw call JSON object>,"response":<raw response JSON object>}`.
When raw call extraction fails and strict logging is enabled, `call` includes:
`"call_parse_error":{"message":"...","raw_body":"..."}`.
`raw_body` is stored as an escaped readable string (not base64), so each line remains valid JSONL.

## Requirements
- Python 3.11+
- `z3-solver`
- `mcp` Python package
- `uvicorn` (ASGI server runtime)

In this workspace, `.venv_sys` already contains runtime dependencies.
For a fresh environment, install `mcp`, `z3-solver`, and `uvicorn` in the Python runtime used to launch the server.

## Run as a server
Start directly:

```bash
/home/node/project/.venv_sys/bin/python3 /home/node/project/logic_mcp_server.py --host 0.0.0.0 --port 8765
```

It serves MCP over streamable HTTP at:
- `http://<host>:<port>/sessions/<sessionId>/`
- `http://<host>:<port>/healthz`

## Use with Codex CLI
Register the server once:

```bash
codex mcp add logic --url http://127.0.0.1:8765/sessions/dev-session-001/
```

Each agent should be installed with its own session URL.

Verify configuration:

```bash
codex mcp list
codex mcp get logic
```

Remove configuration:

```bash
codex mcp remove logic
```

## Quick container bootstrap
Run from the target project root (`$PWD` should be the project where agents run):

```bash
./install_logic_mcp.sh dev-session-001 http://127.0.0.1:8765
```

The installer performs all of the following:
- Registers Codex MCP server `logic` using `--url <base-url>/sessions/<session-id>/`
- Installs `.logic_mcp_manifest` into the target project
- Appends `.logic_mcp_manifest/AGENTS.snippet.md` into `AGENTS.md` (idempotent)

Optional environment overrides:
- `PROJECT_DIR`: target project root (default: current `PWD`)
- `LOGIC_MCP_BASE_URL`: base network URL for the running server (default: `http://127.0.0.1:8765`)
- `LOGIC_STRICT_RAW_CALL_LOGGING`: `1`/`0` toggle for parse-error logging policy (default: `1`)
- `MANIFEST_SOURCE_DIR`: source manifest directory (default: script-local `.logic_mcp_manifest`)
- `MANIFEST_TARGET_DIR`: destination manifest directory (default: `$PROJECT_DIR/.logic_mcp_manifest`)
- `AGENTS_FILE`: target `AGENTS.md` path (default: `$PROJECT_DIR/AGENTS.md`)
- `SERVER_PATH`: override server script path

## Validation Scripts
- `scripts/integration_test_http.sh`: MCP initialize/call flow, session isolation, compact/full list behavior, and log fidelity.
- `scripts/test_audit_parse_error.sh`: strict parse-error logging behavior and JSONL-safe escaped raw body.

## Agent-facing files
The manifest install places:
- `.logic_mcp_manifest/manifest.md`
- `.logic_mcp_manifest/examples.md`
- `.logic_mcp_manifest/AGENTS.snippet.md`

Files expected at project root after install:
- `AGENTS.md` (contains the Logic MCP snippet)

## Tools exposed
The server exposes 9 tools:
- `logic_set_rule`
- `logic_remove_rule`
- `logic_set_bundle`
- `logic_remove_bundle`
- `logic_set_expectation`
- `logic_remove_expectation`
- `logic_check`
- `logic_context_patch`
- `logic_list`

## Request shapes
MCP already carries tool name, so the simplest payloads do not need a `tool` field.

### `logic_set_bundle`
```json
{
  "id": "decl_times",
  "bundle": [
    "(declare-const start_time_sim1_ms Int)",
    "(declare-const start_time_sim2_ms Int)"
  ]
}
```

### `logic_set_rule`
```json
{
  "id": "sim_start_exact",
  "lang": "pyexpr",
  "rule": "start_time_sim1_ms == start_time_sim2_ms"
}
```

### `logic_set_expectation`
```json
{
  "id": "exp_exact_implies_nonneg",
  "kind": "entails",
  "a_ref": "sim_start_exact",
  "b_ref": "sim_start_exact"
}
```

### `logic_check`
```json
{
  "hypothesis": {
    "facts": {
      "start_time_sim1_ms": 0,
      "start_time_sim2_ms": "?t2"
    },
    "patch": {
      "set_rules": {
        "sim1_first": {
          "lang": "pyexpr",
          "rule": "start_time_sim2_ms > start_time_sim1_ms"
        }
      },
      "remove_rules": []
    }
  },
  "detail_level": "compact"
}
```

### `logic_context_patch`
```json
{
  "ops": [
    {
      "op": "set_code_binding",
      "id": "cb_pricing",
      "set": {
        "path": "src/pricing.py",
        "related_rule_ids": ["sim_start_exact"],
        "related_expectation_ids": [],
        "related_concept_ids": []
      }
    }
  ]
}
```

### `logic_list`
```json
{
  "show": ["all"],
  "detail_level": "compact",
  "limit": 50
}
```

Single-ID lookup:
```json
{
  "id": "sim_start_exact"
}
```

## Result format
All tools return:
- Success: `{ "ok": true }` or `{ "ok": true, "result": { ... } }`
- Failure: `{ "ok": false, "error": { "code": "...", "message": "...", "details": { ... } } }`

## Quick workflow in Codex
1. Add declarations with `logic_set_bundle`.
2. Add persistent invariants with `logic_set_rule`.
3. Add omission checks with `logic_set_expectation`.
4. Add context links with `logic_context_patch`.
5. Run what-if checks via `logic_check` with temporary patch/facts overlays.
6. Inspect compact/full inventories via `logic_list`.

## Compliance notes
The accepted reduced-surface contract is documented in `context-feature-spec.md`.
