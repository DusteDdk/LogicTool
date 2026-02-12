#!/bin/bash
  APT_GET=apt-get
SERVER_NAME="logic"
SESSION_ID="${1:-${LOGIC_SESSION_ID:-}}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVER_PATH="${SERVER_PATH:-$SCRIPT_DIR/logic_mcp_server.py}"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv_logic_mcp}"
PROJECT_DIR="${PROJECT_DIR:-$PWD}"
MANIFEST_SOURCE_DIR="${MANIFEST_SOURCE_DIR:-$SCRIPT_DIR/.logic_mcp_manifest}"
MANIFEST_TARGET_DIR="${MANIFEST_TARGET_DIR:-$PROJECT_DIR/.logic_mcp_manifest}"
AGENTS_FILE="${AGENTS_FILE:-$PROJECT_DIR/AGENTS.md}"
CONTEXT_MAP_FILE="${CONTEXT_MAP_FILE:-$PROJECT_DIR/.logic_context_map.md}"

echo "==> Creating/updating Python virtualenv: $VENV_DIR"
# Use system site packages so Ubuntu's prebuilt python3-z3 can be reused.
python3 -m venv --system-site-packages "$VENV_DIR"
"$VENV_DIR/bin/python3" -m pip install --upgrade pip
"$VENV_DIR/bin/python3" -m pip install --upgrade mcp
