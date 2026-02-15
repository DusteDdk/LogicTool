#!/usr/bin/env bash
set -euo pipefail

# End-to-end smoke test for the HTTP-only Logic MCP server (v5 surface).
# Validates MCP flow, per-session isolation, list/read behavior, context patch,
# and log fidelity.

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv_sys/bin/python3}"
SERVER_SCRIPT="$ROOT_DIR/logic_mcp_server.py"
PORT="${PORT:-8890}"
BASE_URL="http://127.0.0.1:${PORT}"

SESSION_A="itest-a"
SESSION_B="itest-b"
STATE_A_DIR="$ROOT_DIR/logic_store/${SESSION_A}"
STATE_B_DIR="$ROOT_DIR/logic_store/${SESSION_B}"
LOG_A="$STATE_A_DIR/log.jsonl"
LOG_B="$STATE_B_DIR/log.jsonl"

rm -f "$LOG_A" "$LOG_B"
rm -rf "$STATE_A_DIR" "$STATE_B_DIR"

"$PYTHON_BIN" "$SERVER_SCRIPT" --host 127.0.0.1 --port "$PORT" >/tmp/logic_mcp_itest.log 2>&1 &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" >/dev/null 2>&1 || true
  wait "$SERVER_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

sleep 1
curl -fsS "$BASE_URL/healthz" >/tmp/logic_mcp_itest_healthz.json

init_session() {
  local sid="$1"
  local url="$BASE_URL/sessions/${sid}/"
  local hdr="/tmp/${sid}_hdr.txt"
  local init_payload='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"itest","version":"1"}}}'

  curl -sS -D "$hdr" -o "/tmp/${sid}_init.txt" -X POST "$url" \
    -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' \
    -H 'mcp-protocol-version: 2025-11-25' \
    --data "$init_payload" >/dev/null

  local mcp_sid
  mcp_sid="$(awk 'BEGIN{IGNORECASE=1} /^mcp-session-id:/ {print $2}' "$hdr" | tr -d '\r')"
  if [[ -z "$mcp_sid" ]]; then
    echo "missing mcp-session-id header for $sid" >&2
    return 1
  fi

  local notif='{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}'
  curl -sS -o "/tmp/${sid}_notif.txt" -X POST "$url" \
    -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' \
    -H 'mcp-protocol-version: 2025-11-25' \
    -H "mcp-session-id: ${mcp_sid}" \
    --data "$notif" >/dev/null

  printf '%s' "$mcp_sid"
}

call_tool() {
  local sid="$1"
  local mcp_sid="$2"
  local payload="$3"
  local out="$4"
  local url="$BASE_URL/sessions/${sid}/"

  curl -sS -o "$out" -X POST "$url" \
    -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' \
    -H 'mcp-protocol-version: 2025-11-25' \
    -H "mcp-session-id: ${mcp_sid}" \
    --data "$payload" >/dev/null
}

MCP_A="$(init_session "$SESSION_A")"
MCP_B="$(init_session "$SESSION_B")"

call_tool "$SESSION_A" "$MCP_A" '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"logic_list"}}' /tmp/itest_a_list.txt
call_tool "$SESSION_A" "$MCP_A" '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"logic_set_rule","arguments":{"id":"rule-a","lang":"pyexpr","rule":"x > 0","intent":"Rule for integration test flow","motivation":{"rationale":"Seed a minimal rule for e2e assertions"}}}}' /tmp/itest_a_set_rule.txt
call_tool "$SESSION_A" "$MCP_A" '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"logic_set_expectation","arguments":{"id":"exp-a","kind":"entails","a_ref":"rule-a","b_ref":"rule-a","motivation":{"rationale":"Guard against accidental expectation shape regressions"}}}}' /tmp/itest_a_set_expect.txt
call_tool "$SESSION_A" "$MCP_A" '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"logic_context_patch","arguments":{"ops":[{"op":"set_code_binding","id":"cb-a","set":{"path":"src/a.py","related_rule_ids":["rule-a"],"related_expectation_ids":[],"related_concept_ids":[],"motivation":{"rationale":"Bind rule to concrete source path"}}}]}}}' /tmp/itest_a_context_patch.txt
call_tool "$SESSION_A" "$MCP_A" '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"logic_list","arguments":{"show":["all"],"detail_level":"more"}}}' /tmp/itest_a_list_more.txt
call_tool "$SESSION_A" "$MCP_A" '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"logic_read","arguments":{"id":"rule-a","detail_level":"full"}}}' /tmp/itest_a_read_rule.txt
call_tool "$SESSION_A" "$MCP_A" '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"logic_list","arguments":{"detail_level":"full"}}}' /tmp/itest_a_list_full_rejected.txt
call_tool "$SESSION_B" "$MCP_B" '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"logic_list"}}' /tmp/itest_b_list.txt

rg -q '"structuredContent":\{"ok":true,"result":\{"items":\[\]\}\}' /tmp/itest_a_list.txt
rg -q '"structuredContent":\{"ok":true\}' /tmp/itest_a_set_rule.txt
rg -q '"structuredContent":\{"ok":true\}' /tmp/itest_a_set_expect.txt
rg -q '"structuredContent":\{"ok":true\}' /tmp/itest_a_context_patch.txt
rg -q '"type":"rule"' /tmp/itest_a_list_more.txt
rg -q '"id":"rule-a"' /tmp/itest_a_list_more.txt
rg -q '"type":"code_binding"' /tmp/itest_a_list_more.txt
rg -q '"structuredContent":\{"ok":true,"result":\{"item":' /tmp/itest_a_read_rule.txt
rg -q '"id":"rule-a"' /tmp/itest_a_read_rule.txt
rg -q '"content":"x > 0"' /tmp/itest_a_read_rule.txt
rg -q "Input validation error: 'full' is not one of \\['minimal', 'compact', 'more'\\]" /tmp/itest_a_list_full_rejected.txt
rg -q '"structuredContent":\{"ok":true,"result":\{"items":\[\]\}\}' /tmp/itest_b_list.txt

[[ -f "$LOG_A" ]] && [[ -f "$LOG_B" ]]
head -n 1 "$LOG_A" | rg -q '"call":\{"name":"logic_list"\}'
rg -q '"call":\{"name":"logic_set_rule"' "$LOG_A"
rg -q '"intent":"Rule for integration test flow"' "$LOG_A"
rg -q '"motivation":\{"rationale":"Seed a minimal rule for e2e assertions"\}' "$LOG_A"
! rg -q 'logic_set_rule' "$LOG_B"

echo "integration test passed"
