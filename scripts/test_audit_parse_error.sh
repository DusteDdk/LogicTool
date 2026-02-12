#!/usr/bin/env bash
set -euo pipefail

# Verifies strict raw call logging includes readable escaped raw_body on parse errors.

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv_sys/bin/python3}"

"$PYTHON_BIN" - <<'PY'
import asyncio
import json

from logic_mcp.audit_log import build_tool_call_payload


class DummyRequest:
    async def body(self):
        # Invalid JSON with newline and byte escape candidate.
        return b'{"jsonrpc":"2.0","params":{"name":"logic_list",\n"arguments":{}}\xff'


payload = asyncio.run(
    build_tool_call_payload(
        DummyRequest(),
        "logic_list",
        None,
        strict_raw_call_logging=True,
    )
)

assert payload.get("name") == "logic_list", payload
assert "call_parse_error" in payload, payload
assert "raw_body" in payload["call_parse_error"], payload

raw_body = payload["call_parse_error"]["raw_body"]
assert isinstance(raw_body, str), payload
assert "\\xff" in raw_body, raw_body

encoded = json.dumps(payload, ensure_ascii=False)
json.loads(encoded)
print(encoded)
PY

echo "audit parse-error test passed"
