#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shlex
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_DIR_NAME = ".logic_mcp"
CONFIG_FILE_NAME = "logic_config.json"
DEFAULT_REMOTE_PORT = 8765
COMMAND_CHANNEL_PATH = "/supervisor/sidecar/ws"
PROJECT_MARKERS = (".logic_mcp", "AGENTS.md", "CLAUDE.md", ".cursor", ".git")
SUPPORTED_CLIENTS = {"codex", "claude"}
LOCAL_VENV_DIR_NAME = ".venv_logicar"
REQUIRED_PYTHON_PACKAGES = ("websockets",)


@dataclass
class RemoteEndpoint:
    raw: str
    host: str
    port: int
    http_scheme: str
    base_path: str
    base_url: str
    ws_url: str
    display: str


@dataclass
class RuntimeState:
    workdir: Path
    config_dir: Path
    config_path: Path
    session: str | None
    tool_url: str | None
    remote: RemoteEndpoint
    local: str
    instance_id: str
    pid: int

    def status_payload(self, msg_type: str) -> dict[str, Any]:
        return {
            "type": msg_type,
            "workdir": str(self.workdir),
            "local": self.local,
            "session": self.session or "",
            "instance_id": self.instance_id,
            "pid": self.pid,
            "remote": self.remote.base_url,
            "tool_url": self.tool_url or "",
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LogiCar sidecar client")
    parser.add_argument("--workdir", help="Path to project workdir.")
    parser.add_argument("--session", help="Session id for tool URL.")
    parser.add_argument("--remote", help="Remote MCP server host:port or base URL.")
    return parser.parse_args()


def _default_port_for_scheme(scheme: str) -> int:
    value = scheme.lower()
    if value in {"https", "wss"}:
        return 443
    return 80


def _format_host_for_url(host: str) -> str:
    try:
        parsed = socket.getaddrinfo(host, None)
        if parsed and ":" in host and not host.startswith("["):
            return f"[{host}]"
    except Exception:
        pass
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _normalize_remote_input(raw_remote: str) -> str:
    value = raw_remote.strip()
    if not value:
        raise ValueError("empty remote")
    if "://" not in value:
        value = f"http://{value}"
    return value


def parse_remote_endpoint(raw_remote: str) -> RemoteEndpoint:
    normalized = _normalize_remote_input(raw_remote)
    parsed = urllib.parse.urlparse(normalized)
    if not parsed.hostname:
        raise ValueError("missing host")

    source_scheme = (parsed.scheme or "http").lower()
    if source_scheme in {"ws", "wss"}:
        http_scheme = "https" if source_scheme == "wss" else "http"
    elif source_scheme in {"http", "https"}:
        http_scheme = source_scheme
    else:
        raise ValueError("unsupported scheme")

    port = parsed.port or _default_port_for_scheme(source_scheme)
    host = parsed.hostname
    assert host is not None
    base_path = parsed.path.rstrip("/")
    host_url = _format_host_for_url(host)

    http_default_port = _default_port_for_scheme(http_scheme)
    http_base = f"{http_scheme}://{host_url}"
    if port != http_default_port:
        http_base = f"{http_base}:{port}"
    if base_path:
        http_base = f"{http_base}{base_path}"

    ws_scheme = "wss" if http_scheme == "https" else "ws"
    ws_default_port = _default_port_for_scheme(ws_scheme)
    ws_base = f"{ws_scheme}://{host_url}"
    if port != ws_default_port:
        ws_base = f"{ws_base}:{port}"
    if base_path:
        ws_base = f"{ws_base}{base_path}"
    ws_url = f"{ws_base}{COMMAND_CHANNEL_PATH}"

    return RemoteEndpoint(
        raw=raw_remote,
        host=host,
        port=port,
        http_scheme=http_scheme,
        base_path=base_path,
        base_url=http_base,
        ws_url=ws_url,
        display=f"{host}:{port}",
    )


def is_running_in_docker() -> bool:
    if Path("/.dockerenv").exists():
        return True
    cgroup = Path("/proc/1/cgroup")
    if not cgroup.exists():
        return False
    try:
        text = cgroup.read_text(encoding="utf-8", errors="replace").lower()
    except Exception:
        return False
    return ("docker" in text) or ("kubepods" in text)


def docker_gateway_ip() -> str:
    try:
        output = subprocess.check_output(
            ["ip", "route", "show", "default"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("default"):
                continue
            parts = line.split()
            if "via" in parts:
                idx = parts.index("via")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
    except Exception:
        pass
    return "172.17.0.1"


def default_remote_endpoint() -> RemoteEndpoint:
    host = docker_gateway_ip() if is_running_in_docker() else "127.0.0.1"
    return parse_remote_endpoint(f"http://{host}:{DEFAULT_REMOTE_PORT}")


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except Exception:
        return {}
    return {}


def save_config(state: RuntimeState) -> None:
    state.config_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "Remote": state.remote.base_url,
    }
    if state.session:
        payload["Session"] = state.session
    if state.tool_url:
        payload["ToolUrl"] = state.tool_url
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    state.config_path.write_text(text, encoding="utf-8")


def compute_tool_url(remote: RemoteEndpoint, session: str | None) -> str | None:
    if not session:
        return None
    safe = session.strip()
    if not safe:
        return None
    encoded = urllib.parse.quote(safe, safe="-_.~")
    return f"{remote.base_url}/sessions/{encoded}/"


def _is_project_like(path: Path) -> bool:
    for marker in PROJECT_MARKERS:
        if (path / marker).exists():
            return True
    return False


def determine_workdir(raw_workdir: str | None) -> Path:
    if raw_workdir:
        return Path(raw_workdir).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if _is_project_like(cwd):
        return cwd
    raise RuntimeError("Error: Provide --workdir <project_path>")


def _venv_python_path(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _run_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )


def _format_subprocess_error(result: subprocess.CompletedProcess[str]) -> str:
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    if stderr:
        return stderr
    if stdout:
        return stdout
    return f"exit_code={result.returncode}"


def _missing_python_packages(python_bin: Path) -> list[str]:
    missing: list[str] = []
    for package in REQUIRED_PYTHON_PACKAGES:
        check = _run_capture([str(python_bin), "-c", f"import {package}"])
        if check.returncode != 0:
            missing.append(package)
    return missing


def _ensure_python_packages(python_bin: Path) -> tuple[bool, str]:
    missing = _missing_python_packages(python_bin)
    if not missing:
        return True, ""

    _run_capture([str(python_bin), "-m", "ensurepip", "--upgrade"])
    install = _run_capture([str(python_bin), "-m", "pip", "install", *missing])
    if install.returncode == 0:
        return True, ""
    return False, _format_subprocess_error(install)


def ensure_local_venv_runtime(raw_workdir: str | None) -> int:
    try:
        workdir = determine_workdir(raw_workdir)
    except Exception:
        return 0

    venv_dir = workdir / LOCAL_VENV_DIR_NAME
    venv_python = _venv_python_path(venv_dir)
    current_prefix = Path(sys.prefix).resolve()
    running_in_target_venv = current_prefix == venv_dir.resolve()

    if not venv_python.exists():
        print(f"Creating LogiCar runtime venv: {venv_dir}")
        created = _run_capture([sys.executable, "-m", "venv", str(venv_dir)])
        if created.returncode != 0:
            print("Error: Could not create .venv_logicar")
            print(_format_subprocess_error(created))
            return 1
    if not venv_python.exists():
        print(f"Error: Missing venv interpreter: {venv_python}")
        return 1

    ok, error = _ensure_python_packages(venv_python)
    if not ok:
        print("Error: Could not install LogiCar dependencies in .venv_logicar")
        if error:
            print(error)
        return 1

    if not running_in_target_venv:
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])
    return 0


def normalize_session(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def resolve_remote(
    cli_remote: str | None,
    config_payload: dict[str, Any],
) -> RemoteEndpoint:
    if isinstance(cli_remote, str) and cli_remote.strip():
        return parse_remote_endpoint(cli_remote.strip())
    raw = config_payload.get("Remote")
    if isinstance(raw, str) and raw.strip():
        try:
            return parse_remote_endpoint(raw.strip())
        except Exception:
            pass
    return default_remote_endpoint()


def infer_local_descriptor(remote_host: str) -> str:
    hostname = socket.gethostname()
    ip = "127.0.0.1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote_host, 80))
        ip = sock.getsockname()[0]
    except Exception:
        try:
            ip = socket.gethostbyname(hostname)
        except Exception:
            ip = "127.0.0.1"
    finally:
        sock.close()
    return f"{hostname} {ip}"


def clip_text(value: str, limit: int = 20000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]..."


def run_command(command: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "command_not_found",
            "command": command,
            "stdout": "",
            "stderr": f"Command not found: {command[0]}",
            "exit_code": 127,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"command_failed: {exc}",
            "command": command,
            "stdout": "",
            "stderr": str(exc),
            "exit_code": 1,
        }
    return {
        "ok": proc.returncode == 0,
        "command": command,
        "stdout": clip_text(proc.stdout or ""),
        "stderr": clip_text(proc.stderr or ""),
        "exit_code": proc.returncode,
    }


def tool_client_command(client: str, action: str, tool_url: str | None) -> list[str]:
    if client not in SUPPORTED_CLIENTS:
        raise ValueError("unsupported_client")
    if action == "add":
        if not tool_url:
            raise ValueError("missing_tool_url")
        return [client, "mcp", "add", "logic", "--url", tool_url]
    if action == "list":
        return [client, "mcp", "list"]
    if action == "remove":
        return [client, "mcp", "remove", "logic"]
    raise ValueError("unsupported_action")


def bootstrap_write_files(state: RuntimeState) -> dict[str, Any]:
    bootstrap_url = f"{state.remote.base_url}/agents/bootstrap"
    try:
        with urllib.request.urlopen(bootstrap_url, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"bootstrap_request_failed: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"bootstrap_request_failed: {exc}"}

    try:
        payload = json.loads(body)
    except Exception as exc:
        return {"ok": False, "error": f"bootstrap_response_not_json: {exc}"}

    urls = payload.get("bootstrap_urls")
    if not isinstance(urls, list):
        return {"ok": False, "error": "bootstrap_urls_missing"}

    written: list[str] = []
    for item in urls:
        if not isinstance(item, str) or not item.strip():
            continue
        source_url = item.strip()
        parsed = urllib.parse.urlparse(source_url)
        filename = Path(parsed.path).name
        if not filename:
            continue
        target = state.workdir / filename
        try:
            with urllib.request.urlopen(source_url, timeout=20) as response:
                data = response.read()
            target.write_bytes(data)
            written.append(str(target))
        except Exception as exc:
            return {
                "ok": False,
                "error": f"bootstrap_write_failed: {exc}",
                "written_files": written,
            }
    return {"ok": True, "written_files": written}


def execute_command_sync(state: RuntimeState, command: str, args: dict[str, Any]) -> dict[str, Any]:
    if command == "set_session":
        raw = args.get("session")
        session = normalize_session(raw)
        state.session = session
        state.tool_url = compute_tool_url(state.remote, state.session)
        save_config(state)
        return {
            "ok": True,
            "session": state.session or "",
            "tool_url": state.tool_url or "",
        }

    if command in {"add_tool", "list_tools", "remove_tool"}:
        client = args.get("client")
        if not isinstance(client, str) or client not in SUPPORTED_CLIENTS:
            return {
                "ok": False,
                "error": "invalid_client",
                "valid_clients": sorted(SUPPORTED_CLIENTS),
            }
        action = "list"
        if command == "add_tool":
            action = "add"
        elif command == "remove_tool":
            action = "remove"
        try:
            cli_command = tool_client_command(client, action, state.tool_url)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        result = run_command(cli_command)
        result["client"] = client
        result["action"] = action
        result["shell"] = " ".join(shlex.quote(part) for part in cli_command)
        return result

    if command == "write_bootstrap":
        return bootstrap_write_files(state)

    return {"ok": False, "error": "unknown_command"}


async def handle_command_message(ws: Any, state: RuntimeState, payload: dict[str, Any]) -> None:
    command_id = payload.get("command_id")
    command = payload.get("command")
    args = payload.get("args")
    if not isinstance(command_id, str) or not command_id:
        return
    if not isinstance(command, str) or not command:
        result_payload = {
            "type": "command_result",
            "command_id": command_id,
            "ok": False,
            "error": "missing_command",
        }
        await ws.send(json.dumps(result_payload))
        return
    args_obj = args if isinstance(args, dict) else {}
    result = await asyncio.to_thread(execute_command_sync, state, command, args_obj)
    result_payload = {
        "type": "command_result",
        "command_id": command_id,
        "command": command,
        "ok": bool(result.get("ok")),
        "result": result,
        "session": state.session or "",
        "tool_url": state.tool_url or "",
    }
    if not result_payload["ok"] and isinstance(result.get("error"), str):
        result_payload["error"] = result["error"]
        result_payload["message"] = result["error"]
    await ws.send(json.dumps(result_payload))
    await ws.send(json.dumps(state.status_payload("status")))


async def heartbeat_loop(ws: Any, state: RuntimeState) -> None:
    while True:
        await asyncio.sleep(20)
        await ws.send(json.dumps(state.status_payload("heartbeat")))


async def run_command_channel(state: RuntimeState) -> int:
    try:
        import websockets  # type: ignore
    except Exception:
        print("Error: Missing dependency 'websockets' in Python environment.")
        return 1
    try:
        async with websockets.connect(state.remote.ws_url, open_timeout=10) as ws:
            await ws.send(json.dumps(state.status_payload("hello")))
            try:
                first = await asyncio.wait_for(ws.recv(), timeout=15)
                first_payload = json.loads(first) if isinstance(first, str) else {}
                if isinstance(first_payload, dict):
                    assigned = first_payload.get("instance_id")
                    if isinstance(assigned, str) and assigned.strip():
                        state.instance_id = assigned.strip()
            except Exception:
                pass
            print(f"Established WS command channel with {state.remote.display}")
            print("Ready.")
            heartbeat = asyncio.create_task(heartbeat_loop(ws, state))
            try:
                while True:
                    raw = await ws.recv()
                    payload = json.loads(raw) if isinstance(raw, str) else {}
                    if not isinstance(payload, dict):
                        continue
                    msg_type = payload.get("type")
                    if msg_type == "command":
                        await handle_command_message(ws, state, payload)
                    elif msg_type == "hello_ack":
                        assigned = payload.get("instance_id")
                        if isinstance(assigned, str) and assigned.strip():
                            state.instance_id = assigned.strip()
                    elif msg_type == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
    except Exception:
        print(f"Error: Couldn't establish WS command-channel to {state.remote.display}")
        return 1
    return 0


def build_runtime(args: argparse.Namespace) -> RuntimeState:
    workdir = determine_workdir(args.workdir)
    config_dir = workdir / CONFIG_DIR_NAME
    config_path = config_dir / CONFIG_FILE_NAME
    config_payload = load_config(config_path)

    session = normalize_session(args.session)
    if session is None:
        session = normalize_session(config_payload.get("Session"))

    try:
        remote = resolve_remote(args.remote, config_payload)
    except Exception as exc:
        raise RuntimeError(f"Error: Invalid --remote value '{args.remote}'.") from exc
    tool_url = compute_tool_url(remote, session)
    if not tool_url:
        raw_tool_url = config_payload.get("ToolUrl")
        if isinstance(raw_tool_url, str) and raw_tool_url.strip():
            tool_url = raw_tool_url.strip()

    local = infer_local_descriptor(remote.host)
    return RuntimeState(
        workdir=workdir,
        config_dir=config_dir,
        config_path=config_path,
        session=session,
        tool_url=tool_url,
        remote=remote,
        local=local,
        instance_id=str(uuid.uuid4()),
        pid=os.getpid(),
    )


def main() -> int:
    args = parse_args()
    bootstrap_rc = ensure_local_venv_runtime(args.workdir)
    if bootstrap_rc != 0:
        return bootstrap_rc

    print("LogiCar startup...")
    try:
        state = build_runtime(args)
    except RuntimeError as exc:
        print(str(exc))
        return 1
    except Exception:
        print("Error: Provide --workdir <project_path>")
        return 1

    try:
        os.chdir(state.workdir)
    except Exception:
        print(f"Error: Could not set {state.workdir} as CWD.")
        return 1
    print(f"Entered workdir: {state.workdir}")

    save_config(state)
    return asyncio.run(run_command_channel(state))


if __name__ == "__main__":
    sys.exit(main())
