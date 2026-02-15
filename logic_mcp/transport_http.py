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

from .errors import LogicError
from .paths import PROJECT_ROOT
from .store import sanitize_namespace
from .supervisor import INTERCEPT_MODES
from .supervisor import SUPERVISOR
from .supervisor import bootstrap_resource_files
from .supervisor import read_recent_logs

SUPERVISOR_ASSETS_DIR = PROJECT_ROOT / "logic_mcp" / "resources" / "supervisor"
BOOTSTRAP_DIR = PROJECT_ROOT / "logic_mcp" / "resources" / "bootstrap"
SIDECAR_ARTIFACT_DIR = PROJECT_ROOT / "LogiCar"
SIDECAR_ALLOWED_COMMANDS = {"set_session", "add_tool", "list_tools", "remove_tool", "write_bootstrap"}
SIDECAR_ALLOWED_CLIENTS = {"codex", "claude"}
LOG_LEVELS = {"debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"}
LOG_WINDOW_SIZE = 10


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
            raw = request.query_params.get("limit", str(LOG_WINDOW_SIZE))
            limit = max(1, min(200, int(raw)))
        except Exception:
            limit = LOG_WINDOW_SIZE
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

    async def api_remove_session(request: Request) -> JSONResponse:
        session_id = sanitize_namespace(request.path_params["session_id"])
        removed, reason = await SUPERVISOR.delete_session_data(session_id)
        if removed:
            return JSONResponse({"ok": True, "deleted": True, "session_id": session_id})
        if reason == "session_not_found":
            return JSONResponse({"ok": False, "error": reason, "session_id": session_id}, status_code=404)
        return JSONResponse({"ok": False, "error": reason, "session_id": session_id}, status_code=500)

    async def api_reset_session(request: Request) -> JSONResponse:
        session_id = sanitize_namespace(request.path_params["session_id"])
        body = await _read_json_body(request)
        wipe_logs = body.get("wipe_logs", True)
        if not isinstance(wipe_logs, bool):
            return JSONResponse({"ok": False, "error": "wipe_logs_must_be_boolean"}, status_code=400)
        try:
            from .engine import get_engine

            response = get_engine(session_id).reset_session({"confirm": "reset-session", "wipe_logs": wipe_logs})
            await SUPERVISOR.publish_session_graph_updated(session_id)
            payload = response.get("result", {}) if isinstance(response, dict) else {}
            return JSONResponse(
                {
                    "ok": True,
                    "session_id": session_id,
                    "wiped_inventory": bool(payload.get("wiped_inventory")),
                    "wiped_logs": bool(payload.get("wiped_logs")),
                }
            )
        except LogicError as exc:
            status = 404 if exc.code == "E_UNKNOWN_ID" else 400
            return JSONResponse(
                {"ok": False, "error": exc.code, "message": exc.message, "details": exc.details},
                status_code=status,
            )
        except Exception:
            return JSONResponse({"ok": False, "error": "session_reset_failed"}, status_code=500)

    async def api_sidecars(_: Request) -> JSONResponse:
        sidecars = await SUPERVISOR.list_sidecars_summary()
        return JSONResponse({"ok": True, "sidecars": sidecars})

    async def api_sidecar_command(request: Request) -> JSONResponse:
        instance_id = request.path_params["instance_id"]
        body = await _read_json_body(request)
        command = body.get("command")
        if not isinstance(command, str) or command not in SIDECAR_ALLOWED_COMMANDS:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "invalid_command",
                    "valid_commands": sorted(SIDECAR_ALLOWED_COMMANDS),
                },
                status_code=400,
            )
        args = body.get("args")
        payload_args = args if isinstance(args, dict) else {}
        if command in {"add_tool", "list_tools", "remove_tool"}:
            client = payload_args.get("client")
            if not isinstance(client, str) or client not in SIDECAR_ALLOWED_CLIENTS:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "invalid_client",
                        "valid_clients": sorted(SIDECAR_ALLOWED_CLIENTS),
                    },
                    status_code=400,
                )
        if command == "set_session":
            session = payload_args.get("session")
            if session is not None and not isinstance(session, str):
                return JSONResponse({"ok": False, "error": "session_must_be_string"}, status_code=400)
            payload_args = {"session": session if isinstance(session, str) else ""}

        ok, reason, result = await SUPERVISOR.send_command_to_sidecar(
            instance_id,
            command=command,
            arguments=payload_args,
        )
        if not ok:
            if reason == "sidecar_not_found":
                return JSONResponse({"ok": False, "error": reason}, status_code=404)
            if reason == "sidecar_offline":
                return JSONResponse({"ok": False, "error": reason}, status_code=409)
            if reason == "sidecar_command_timeout":
                return JSONResponse({"ok": False, "error": reason}, status_code=504)
            if reason == "sidecar_command_invalid":
                return JSONResponse({"ok": False, "error": reason}, status_code=400)
            return JSONResponse({"ok": False, "error": reason}, status_code=500)
        return JSONResponse({"ok": True, "instance_id": instance_id, "result": result or {}})

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

    async def sidecar_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        instance_id = ""
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), timeout=15.0)
            if not isinstance(hello, dict) or hello.get("type") != "hello":
                await websocket.close(code=4400)
                return
            registration = await SUPERVISOR.register_sidecar_connection(
                instance_id=str(hello.get("instance_id") or ""),
                connection=websocket,
                workdir=hello.get("workdir"),
                local=hello.get("local"),
                session_id=hello.get("session"),
                pid=hello.get("pid"),
                remote=hello.get("remote"),
                tool_url=hello.get("tool_url"),
            )
            instance_id = str(registration.get("instance_id") or "")
            await websocket.send_json({"type": "hello_ack", "instance_id": instance_id})
            while True:
                payload = await websocket.receive_json()
                if not isinstance(payload, dict):
                    continue
                msg_type = payload.get("type")
                if msg_type in {"heartbeat", "status", "hello"}:
                    await SUPERVISOR.update_sidecar_connection(
                        instance_id,
                        workdir=payload.get("workdir"),
                        local=payload.get("local"),
                        session_id=payload.get("session"),
                        pid=payload.get("pid"),
                        remote=payload.get("remote"),
                        tool_url=payload.get("tool_url"),
                    )
                    if msg_type == "hello":
                        await websocket.send_json({"type": "hello_ack", "instance_id": instance_id})
                    continue
                if msg_type == "command_result":
                    command_id = payload.get("command_id")
                    if isinstance(command_id, str) and command_id:
                        await SUPERVISOR.resolve_sidecar_command(instance_id, command_id, payload)
                    continue
                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if instance_id:
                await SUPERVISOR.mark_sidecar_disconnected(instance_id)

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

    async def agents_sidecar_bootstrap(request: Request) -> JSONResponse:
        files = bootstrap_resource_files(SIDECAR_ARTIFACT_DIR)
        base_url = _bootstrap_base_url_from_request(request)
        urls = [f"{base_url}/agents/bootstrap/sidecar/{name}" for name in files]
        payload = {
            "instruction": "Download the LogiCar sidecar artifact and run it in the client project environment.",
            "download_strategy": "Download from artifact_urls and execute the artifact file directly.",
            "artifact_urls": urls,
        }
        return JSONResponse(payload)

    async def agents_sidecar_resource(request: Request) -> FileResponse:
        filename = request.path_params["filename"]
        target = (SIDECAR_ARTIFACT_DIR / filename).resolve()
        if SIDECAR_ARTIFACT_DIR.resolve() not in target.parents or not target.is_file():
            return JSONResponse({"ok": False, "error": "sidecar_resource_not_found"}, status_code=404)
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
            Route("/supervisor/api/sessions/{session_id:str}/reset", endpoint=api_reset_session, methods=["POST"]),
            Route("/supervisor/api/sessions/{session_id:str}", endpoint=api_remove_session, methods=["DELETE"]),
            Route("/supervisor/api/sidecars", endpoint=api_sidecars, methods=["GET"]),
            Route("/supervisor/api/sidecars/{instance_id:str}/command", endpoint=api_sidecar_command, methods=["POST"]),
            Route("/agents/bootstrap", endpoint=agents_bootstrap, methods=["GET"]),
            Route(
                "/agents/bootstrap/resources/{filename:path}",
                endpoint=agents_bootstrap_resource,
                methods=["GET"],
            ),
            Route("/agents/bootstrap/sidecar/", endpoint=agents_sidecar_bootstrap, methods=["GET"]),
            Route("/agents/bootstrap/sidecar/{filename:path}", endpoint=agents_sidecar_resource, methods=["GET"]),
            Route("/sessions/{session_id:str}", endpoint=streamable_http_app),
            Route("/sessions/{session_id:str}/", endpoint=streamable_http_app),
            WebSocketRoute("/supervisor/ws", endpoint=supervisor_ws),
            WebSocketRoute("/supervisor/sidecar/ws", endpoint=sidecar_ws),
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
    sidecars = await SUPERVISOR.list_sidecars_summary()
    sessions = await SUPERVISOR.list_sessions_summary()
    logs_by_session: dict[str, list[dict[str, Any]]] = {}
    session_ids: list[str] = []
    for row in sessions:
        session_id = row.get("session_id")
        if isinstance(session_id, str):
            session_ids.append(session_id)
            logs_by_session[session_id] = read_recent_logs(session_id, limit=LOG_WINDOW_SIZE)
    graphs_by_session = await SUPERVISOR.get_graphs_by_session(session_ids)
    return {
        "sidecars": sidecars,
        "sessions": sessions,
        "logs_by_session": logs_by_session,
        "graphs_by_session": graphs_by_session,
    }


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
