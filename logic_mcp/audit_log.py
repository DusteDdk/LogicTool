from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .paths import STORE_DIR
from .store import sanitize_namespace

LOG_LOCK = threading.Lock()
MAX_RAW_BODY_CHARS = 20000


def _log_timestamp() -> str:
    now = time.time()
    whole = int(now)
    ms = int((now - whole) * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)) + f".{ms:03d}Z"


def _to_log_json(value: Any) -> str:
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(value), separators=(",", ":"), ensure_ascii=False)


def _safe_session_log_name(session_id: str) -> str:
    safe_session_id = sanitize_namespace(session_id)
    if safe_session_id in (".", ".."):
        return "default"
    return safe_session_id


def _session_log_file(session_id: str) -> Path:
    safe = _safe_session_log_name(session_id)
    return STORE_DIR / safe / "log.jsonl"


def _raw_body_to_text(raw_body: bytes) -> str:
    text = raw_body.decode("utf-8", "backslashreplace")
    if len(text) <= MAX_RAW_BODY_CHARS:
        return text
    return f"{text[:MAX_RAW_BODY_CHARS]}...(truncated {len(text) - MAX_RAW_BODY_CHARS} chars)"


def _with_parse_error(
    payload: dict[str, Any],
    *,
    message: str,
    raw_body: Optional[bytes],
    arguments: dict | None,
    strict_raw_call_logging: bool,
) -> dict[str, Any]:
    if not strict_raw_call_logging:
        if arguments is not None:
            payload["arguments"] = arguments
        return payload

    error_obj: dict[str, Any] = {"message": message}
    if raw_body is not None:
        error_obj["raw_body"] = _raw_body_to_text(raw_body)
    payload["call_parse_error"] = error_obj
    if arguments is not None:
        payload["arguments"] = arguments
    return payload


async def build_tool_call_payload(
    request: Any,
    name: str,
    arguments: dict | None,
    *,
    strict_raw_call_logging: bool = True,
) -> dict[str, Any]:
    """Build call payload from raw HTTP JSON-RPC body.

    When strict mode is enabled and body parsing fails, include `call_parse_error`
    with a readable escaped `raw_body` field.
    """
    payload: dict[str, Any] = {"name": name}

    if request is None:
        if arguments is not None:
            payload["arguments"] = arguments
        return payload

    raw_body: Optional[bytes] = None
    try:
        raw_body = await request.body()
    except Exception as exc:
        return _with_parse_error(
            payload,
            message=f"failed to read request body: {exc}",
            raw_body=None,
            arguments=arguments,
            strict_raw_call_logging=strict_raw_call_logging,
        )

    try:
        raw_json = json.loads(raw_body)
    except Exception as exc:
        return _with_parse_error(
            payload,
            message=f"invalid JSON request body: {exc}",
            raw_body=raw_body,
            arguments=arguments,
            strict_raw_call_logging=strict_raw_call_logging,
        )

    if not isinstance(raw_json, dict):
        return _with_parse_error(
            payload,
            message="request body is not a JSON object",
            raw_body=raw_body,
            arguments=arguments,
            strict_raw_call_logging=strict_raw_call_logging,
        )

    params = raw_json.get("params")
    if not isinstance(params, dict):
        return _with_parse_error(
            payload,
            message="request params is not an object",
            raw_body=raw_body,
            arguments=arguments,
            strict_raw_call_logging=strict_raw_call_logging,
        )

    raw_name = params.get("name")
    if isinstance(raw_name, str) and raw_name:
        payload["name"] = raw_name
    if "arguments" in params:
        payload["arguments"] = params.get("arguments")
    return payload


def append_tool_log(
    session_id: str,
    call_payload: Any,
    response_payload: Any,
    *,
    request_duration_ms: int | None = None,
) -> dict[str, Any] | None:
    entry = {
        "time": _log_timestamp(),
        "call": call_payload,
        "response": response_payload,
    }
    if request_duration_ms is not None:
        entry["request_duration_ms"] = max(0, int(request_duration_ms))
    line = _to_log_json(entry) + "\n"
    log_file = _session_log_file(session_id)
    try:
        with LOG_LOCK:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as f:
                f.write(line)
        return entry
    except Exception:
        # Logging must never block tool execution.
        return None
