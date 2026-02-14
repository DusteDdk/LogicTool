from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import asynccontextmanager
from ipaddress import ip_address
from typing import Any, AsyncIterator
import time

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse
from starlette.responses import HTMLResponse
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket
from starlette.websockets import WebSocketDisconnect

from .paths import PROJECT_ROOT
from .store import sanitize_namespace
from .supervisor import INTERCEPT_MODES
from .supervisor import SUPERVISOR
from .supervisor import bootstrap_resource_files
from .supervisor import read_recent_logs

SUPERVISOR_ASSETS_DIR = PROJECT_ROOT / "logic_mcp" / "resources" / "supervisor"
BOOTSTRAP_DIR = PROJECT_ROOT / "logic_mcp" / "resources" / "bootstrap"
LOG_LEVELS = {"debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"}


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

    async def supervisor_page(_: Request) -> HTMLResponse:
        index_file = SUPERVISOR_ASSETS_DIR / "index.html"
        if not index_file.exists():
            return HTMLResponse("<h1>Supervisor dashboard not installed.</h1>", status_code=404)
        return HTMLResponse(index_file.read_text(encoding="utf-8"))

    async def supervisor_asset(request: Request) -> FileResponse:
        rel = request.path_params.get("asset_path", "")
        file_path = (SUPERVISOR_ASSETS_DIR / rel).resolve()
        if SUPERVISOR_ASSETS_DIR.resolve() not in file_path.parents or not file_path.is_file():
            return JSONResponse({"ok": False, "error": "asset_not_found"}, status_code=404)
        return FileResponse(file_path)

    async def api_sessions(_: Request) -> JSONResponse:
        sessions = await SUPERVISOR.list_sessions_summary()
        return JSONResponse({"ok": True, "sessions": sessions})

    async def api_session_logs(request: Request) -> JSONResponse:
        session_id = sanitize_namespace(request.path_params["session_id"])
        try:
            raw = request.query_params.get("limit", "15")
            limit = max(1, min(200, int(raw)))
        except Exception:
            limit = 15
        logs = read_recent_logs(session_id, limit=limit)
        pending = await SUPERVISOR.get_pending_for_session(session_id)
        mode = await SUPERVISOR.get_mode(session_id)
        return JSONResponse(
            {
                "ok": True,
                "session_id": session_id,
                "intercept_mode": mode,
                "pending_intercepts": pending,
                "logs": logs,
            }
        )

    async def api_set_mode(request: Request) -> JSONResponse:
        session_id = sanitize_namespace(request.path_params["session_id"])
        body = await _read_json_body(request)
        mode = body.get("mode")
        if mode not in INTERCEPT_MODES:
            return JSONResponse({"ok": False, "error": "invalid_mode", "valid_modes": list(INTERCEPT_MODES)}, status_code=400)
        updated = await SUPERVISOR.set_mode(session_id, mode)
        return JSONResponse({"ok": True, "session_id": session_id, "intercept_mode": updated})

    async def api_intercept_forward(request: Request) -> JSONResponse:
        intercept_id = request.path_params["intercept_id"]
        body = await _read_json_body(request)
        arguments = body.get("arguments")
        payload = {"arguments": arguments} if isinstance(arguments, dict) else None
        ok = await SUPERVISOR.resolve_from_operator(intercept_id, "forward", payload)
        if not ok:
            return JSONResponse({"ok": False, "error": "intercept_not_found"}, status_code=404)
        return JSONResponse({"ok": True})

    async def api_intercept_override(request: Request) -> JSONResponse:
        intercept_id = request.path_params["intercept_id"]
        body = await _read_json_body(request)
        response = body.get("response")
        if not isinstance(response, dict):
            return JSONResponse({"ok": False, "error": "response_must_be_object"}, status_code=400)
        ok = await SUPERVISOR.resolve_from_operator(intercept_id, "override", {"response": response})
        if not ok:
            return JSONResponse({"ok": False, "error": "intercept_not_found"}, status_code=404)
        return JSONResponse({"ok": True})

    async def api_intercept_send(request: Request) -> JSONResponse:
        intercept_id = request.path_params["intercept_id"]
        body = await _read_json_body(request)
        response = body.get("response")
        if not isinstance(response, dict):
            return JSONResponse({"ok": False, "error": "response_must_be_object"}, status_code=400)
        ok = await SUPERVISOR.resolve_from_operator(intercept_id, "send", {"response": response})
        if not ok:
            return JSONResponse({"ok": False, "error": "intercept_not_found"}, status_code=404)
        return JSONResponse({"ok": True})

    async def api_send_session_message(request: Request) -> JSONResponse:
        session_id = sanitize_namespace(request.path_params["session_id"])
        body = await _read_json_body(request)
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return JSONResponse({"ok": False, "error": "message_required"}, status_code=400)
        level = body.get("level")
        level_value = str(level).lower() if isinstance(level, str) else "info"
        if level_value not in LOG_LEVELS:
            return JSONResponse({"ok": False, "error": "invalid_level", "valid_levels": sorted(LOG_LEVELS)}, status_code=400)
        title = body.get("title") if isinstance(body.get("title"), str) else ""
        source = body.get("source") if isinstance(body.get("source"), str) else "supervisor"
        raw_tags = body.get("tags")
        tags = [value for value in raw_tags if isinstance(value, str)] if isinstance(raw_tags, list) else []
        context = body.get("context") if isinstance(body.get("context"), dict) else {}
        payload = {
            "message": message.strip(),
            "title": title.strip(),
            "source": source.strip() or "supervisor",
            "tags": tags,
            "context": context,
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": session_id,
        }
        delivered, reason = await SUPERVISOR.send_log_message_to_session(
            session_id,
            level=level_value,
            data=payload,
            logger_name="logic.supervisor",
        )
        if not delivered:
            return JSONResponse(
                {
                    "ok": True,
                    "delivered": False,
                    "reason": reason,
                    "session_id": session_id,
                }
            )
        return JSONResponse(
            {
                "ok": True,
                "delivered": True,
                "session_id": session_id,
                "sent_at": payload["sent_at"],
            }
        )

    async def supervisor_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = await SUPERVISOR.subscribe()
        try:
            await websocket.send_json({"type": "snapshot", "data": await _build_ws_snapshot()})
            while True:
                event = await queue.get()
                event_type = event.get("type", "message")
                event_data = event.get("data", {})
                await websocket.send_json(
                    {
                        "type": "event",
                        "event": str(event_type),
                        "data": event_data if isinstance(event_data, dict) else {"value": event_data},
                    }
                )
                await websocket.send_json({"type": "snapshot", "data": await _build_ws_snapshot()})
        except WebSocketDisconnect:
            pass
        finally:
            await SUPERVISOR.unsubscribe(queue)

    async def agents_bootstrap(request: Request) -> JSONResponse:
        files = bootstrap_resource_files(BOOTSTRAP_DIR)
        base_url = _bootstrap_base_url_from_request(request)
        urls = [f"{base_url}/agents/bootstrap/resources/{name}" for name in files]
        payload = {
            "instruction": (
                "Save these files in .cursor/rules or other relevant locations to optimize Logic MCP tool use."
            ),
            "download_strategy": "Download each URL and write files using the original filename from the URL path.",
            "bootstrap_urls": urls,
        }
        return JSONResponse(payload)

    async def agents_bootstrap_resource(request: Request) -> FileResponse:
        filename = request.path_params["filename"]
        target = (BOOTSTRAP_DIR / filename).resolve()
        if BOOTSTRAP_DIR.resolve() not in target.parents or not target.is_file():
            return JSONResponse({"ok": False, "error": "bootstrap_resource_not_found"}, status_code=404)
        return FileResponse(target)

    return Starlette(
        routes=[
            Route("/healthz", endpoint=healthz, methods=["GET"]),
            Route("/supervisor", endpoint=supervisor_page, methods=["GET"]),
            Route("/supervisor/assets/{asset_path:path}", endpoint=supervisor_asset, methods=["GET"]),
            Route("/supervisor/api/sessions", endpoint=api_sessions, methods=["GET"]),
            Route("/supervisor/api/sessions/{session_id:str}/logs", endpoint=api_session_logs, methods=["GET"]),
            Route("/supervisor/api/sessions/{session_id:str}/intercept-mode", endpoint=api_set_mode, methods=["POST"]),
            Route("/supervisor/api/intercepts/{intercept_id:str}/forward", endpoint=api_intercept_forward, methods=["POST"]),
            Route("/supervisor/api/intercepts/{intercept_id:str}/override", endpoint=api_intercept_override, methods=["POST"]),
            Route("/supervisor/api/intercepts/{intercept_id:str}/send", endpoint=api_intercept_send, methods=["POST"]),
            Route("/supervisor/api/sessions/{session_id:str}/messages", endpoint=api_send_session_message, methods=["POST"]),
            Route("/agents/bootstrap", endpoint=agents_bootstrap, methods=["GET"]),
            Route(
                "/agents/bootstrap/resources/{filename:path}",
                endpoint=agents_bootstrap_resource,
                methods=["GET"],
            ),
            Route("/sessions/{session_id:str}", endpoint=streamable_http_app),
            Route("/sessions/{session_id:str}/", endpoint=streamable_http_app),
            WebSocketRoute("/supervisor/ws", endpoint=supervisor_ws),
        ],
        lifespan=lifespan,
    )


async def _read_json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


async def _build_ws_snapshot() -> dict[str, Any]:
    sessions = await SUPERVISOR.list_sessions_summary()
    logs_by_session: dict[str, list[dict[str, Any]]] = {}
    session_ids: list[str] = []
    for row in sessions:
        session_id = row.get("session_id")
        if isinstance(session_id, str):
            session_ids.append(session_id)
            logs_by_session[session_id] = read_recent_logs(session_id, limit=15)
    graphs_by_session = await SUPERVISOR.get_graphs_by_session(session_ids)
    return {"sessions": sessions, "logs_by_session": logs_by_session, "graphs_by_session": graphs_by_session}


def _bootstrap_base_url_from_request(request: Request) -> str:
    scope = request.scope if isinstance(request.scope, dict) else {}
    server_info = scope.get("server")
    scheme = request.url.scheme
    if isinstance(server_info, (tuple, list)) and len(server_info) >= 2:
        host = str(server_info[0])
        port = int(server_info[1])
    else:
        host = request.url.hostname or "127.0.0.1"
        port = request.url.port or (443 if scheme == "https" else 80)
    host_for_url = _format_host_for_url(host)
    default_port = 443 if scheme == "https" else 80
    if port == default_port:
        return f"{scheme}://{host_for_url}"
    return f"{scheme}://{host_for_url}:{port}"


def _format_host_for_url(host: str) -> str:
    try:
        parsed = ip_address(host)
        if parsed.version == 6:
            return f"[{host}]"
        return host
    except ValueError:
        return host


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
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    print(f"[logic-mcp] Supervisor dashboard: http://{display_host}:{port}/supervisor")
    uvicorn_server = uvicorn.Server(uvicorn.Config(app=app, host=host, port=port, log_level=log_level))
    await uvicorn_server.serve()


def run_cli(server: Any) -> None:
    args = parse_args()
    asyncio.run(run_streamable_http_server(server, args.host, args.port, args.log_level))
