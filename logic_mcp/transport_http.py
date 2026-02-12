from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


class StreamableHTTPASGIApp:
    """ASGI adapter that forwards requests to MCP streamable HTTP session manager."""

    def __init__(self, session_manager: StreamableHTTPSessionManager):
        self.session_manager = session_manager

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        await self.session_manager.handle_request(scope, receive, send)


def create_http_app(server: Any) -> Starlette:
    session_manager = StreamableHTTPSessionManager(app=server)
    streamable_http_app = StreamableHTTPASGIApp(session_manager)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "service": "logic-mcp-server"})

    return Starlette(
        routes=[
            Route("/healthz", endpoint=healthz, methods=["GET"]),
            Route("/sessions/{session_id:str}", endpoint=streamable_http_app),
            Route("/sessions/{session_id:str}/", endpoint=streamable_http_app),
        ],
        lifespan=lifespan,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Logic MCP server")
    parser.add_argument(
        "--host",
        default=os.getenv("LOGIC_HTTP_HOST", "0.0.0.0"),
        help="HTTP bind host.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("LOGIC_HTTP_PORT", "8765")),
        help="HTTP bind port.",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOGIC_HTTP_LOG_LEVEL", "info"),
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="HTTP server log level.",
    )
    return parser.parse_args()


async def run_streamable_http_server(server: Any, host: str, port: int, log_level: str) -> None:
    app = create_http_app(server)
    uvicorn_server = uvicorn.Server(uvicorn.Config(app=app, host=host, port=port, log_level=log_level))
    await uvicorn_server.serve()


def run_cli(server: Any) -> None:
    args = parse_args()
    asyncio.run(run_streamable_http_server(server, args.host, args.port, args.log_level))
