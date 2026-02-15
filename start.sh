#!/bin/bash

echo "Starting logic MCP server..."
.venv_logic_mcp/bin/python3 logic_mcp_server.py
exit $?
