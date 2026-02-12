#!/usr/bin/env bash
set -euo pipefail

# Register a networked Logic MCP endpoint in Codex and install
# agent-facing manifest files into the target project directory.

SERVER_NAME="logic"
SESSION_ID="${1:-${LOGIC_SESSION_ID:-}}"
BASE_URL="${2:-${LOGIC_MCP_BASE_URL:-http://127.0.0.1:8765}}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVER_PATH="${SERVER_PATH:-$SCRIPT_DIR/logic_mcp_server.py}"
PROJECT_DIR="${PROJECT_DIR:-$PWD}"
MANIFEST_SOURCE_DIR="${MANIFEST_SOURCE_DIR:-$SCRIPT_DIR/.logic_mcp_manifest}"
MANIFEST_TARGET_DIR="${MANIFEST_TARGET_DIR:-$PROJECT_DIR/.logic_mcp_manifest}"
AGENTS_FILE="${AGENTS_FILE:-$PROJECT_DIR/AGENTS.md}"

if [[ -z "$SESSION_ID" ]]; then
  echo "usage: $0 <session-identifier> [base-url]"
  echo "example: $0 dev-session-001 http://10.0.0.8:8765"
  exit 1
fi

if [[ "$SESSION_ID" == *"/"* ]]; then
  echo "error: session identifier must not contain '/'"
  exit 1
fi

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "error: project directory not found at $PROJECT_DIR"
  exit 1
fi

PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
MANIFEST_TARGET_DIR="$(cd "$(dirname "$MANIFEST_TARGET_DIR")" && pwd)/$(basename "$MANIFEST_TARGET_DIR")"
SERVER_URL="${BASE_URL%/}/sessions/${SESSION_ID}/"

if ! command -v codex >/dev/null 2>&1; then
  echo "error: codex CLI is not installed or not on PATH"
  exit 1
fi

echo "==> Registering MCP server in Codex as '$SERVER_NAME'"
echo "    URL: $SERVER_URL"
if codex mcp get "$SERVER_NAME" >/dev/null 2>&1; then
  codex mcp remove "$SERVER_NAME" >/dev/null 2>&1 || true
fi
codex mcp add "$SERVER_NAME" --url "$SERVER_URL"

echo "==> Installing Logic MCP manifest files into project: $PROJECT_DIR"
if [[ -d "$MANIFEST_SOURCE_DIR" ]]; then
  source_canon="$(cd "$MANIFEST_SOURCE_DIR" && pwd)"
  mkdir -p "$MANIFEST_TARGET_DIR"
  target_canon="$(cd "$(dirname "$MANIFEST_TARGET_DIR")" && pwd)/$(basename "$MANIFEST_TARGET_DIR")"

  if [[ "$source_canon" != "$target_canon" ]]; then
    cp -a "$MANIFEST_SOURCE_DIR"/. "$MANIFEST_TARGET_DIR"/
  fi

  snippet_file="$MANIFEST_TARGET_DIR/AGENTS.snippet.md"
  if [[ -f "$snippet_file" ]]; then
    touch "$AGENTS_FILE"
    if ! grep -Fqx "## Logic MCP" "$AGENTS_FILE"; then
      printf "\n" >>"$AGENTS_FILE"
      cat "$snippet_file" >>"$AGENTS_FILE"
      printf "\n" >>"$AGENTS_FILE"
    fi
  else
    echo "warning: AGENTS snippet not found at $snippet_file"
  fi
else
  echo "warning: manifest source directory not found at $MANIFEST_SOURCE_DIR"
fi

echo
echo "Done."
echo "Session identifier: $SESSION_ID"
echo "Session URL: $SERVER_URL"
echo "Project directory: $PROJECT_DIR"
echo "Manifest directory: $MANIFEST_TARGET_DIR"
echo "AGENTS file: $AGENTS_FILE"
if [[ -f "$SERVER_PATH" ]]; then
  echo "Server script: $SERVER_PATH"
fi
echo "Use it with:"
echo "  codex mcp get $SERVER_NAME"
echo "  codex mcp list"
echo
codex mcp get "$SERVER_NAME"
