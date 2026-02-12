#!/usr/bin/env python3
"""Logic MCP server entrypoint (HTTP only)."""

from __future__ import annotations

from logic_mcp.engine import server
from logic_mcp.transport_http import run_cli


if __name__ == "__main__":
    run_cli(server)
