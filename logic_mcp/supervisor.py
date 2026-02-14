from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit_log import _safe_session_log_name
from .paths import STORE_DIR
from .session_graph import build_session_graph_svg
from .store import sanitize_namespace

INTERCEPT_DISABLED = "disabled"
INTERCEPT_CALL = "call"
INTERCEPT_REPLY = "reply"
INTERCEPT_CALL_AND_REPLY = "call_and_reply"
INTERCEPT_MODES = (
    INTERCEPT_DISABLED,
    INTERCEPT_CALL,
    INTERCEPT_REPLY,
    INTERCEPT_CALL_AND_REPLY,
)
INTERCEPT_STAGE_CALL = "call"
INTERCEPT_STAGE_REPLY = "reply"


def _now_ts() -> float:
    return asyncio.get_running_loop().time()


def _safe_json(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        return str(value)


def _iso_from_epoch(epoch_seconds: float) -> str:
    import time

    whole = int(epoch_seconds)
    ms = int((epoch_seconds - whole) * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole)) + f".{ms:03d}Z"


@dataclass
class PendingIntercept:
    intercept_id: str
    session_id: str
    stage: str
    tool_name: str
    created_monotonic: float
    timeout_seconds: float
    call_payload: dict[str, Any]
    tool_arguments: dict[str, Any]
    output_schema: dict[str, Any] | None
    tool_response: dict[str, Any] | None
    operator_payload: dict[str, Any] | None = None
    operator_action: str | None = None
    decision_event: asyncio.Event | None = None

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "intercept_id": self.intercept_id,
            "session_id": self.session_id,
            "stage": self.stage,
            "tool_name": self.tool_name,
            "created_monotonic": self.created_monotonic,
            "timeout_seconds": self.timeout_seconds,
            "call_payload": _safe_json(self.call_payload),
            "tool_arguments": _safe_json(self.tool_arguments),
            "output_schema": _safe_json(self.output_schema),
            "tool_response": _safe_json(self.tool_response),
            "operator_payload": _safe_json(self.operator_payload),
            "operator_action": self.operator_action,
        }


@dataclass
class ActiveSessionTarget:
    session_id: str
    session_obj: Any
    last_seen_epoch: float
    ref_id: str

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "last_seen_epoch": self.last_seen_epoch,
            "ref_id": self.ref_id,
            "can_send_message": True,
        }


class SupervisorCoordinator:
    def __init__(self) -> None:
        self._modes: dict[str, str] = {}
        self._pending: dict[str, PendingIntercept] = {}
        self._active_targets: dict[str, dict[str, ActiveSessionTarget]] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._timeout_seconds = float(os.getenv("LOGIC_SUPERVISOR_INTERCEPT_TIMEOUT_SEC", "600"))

    async def set_mode(self, session_id: str, mode: str) -> str:
        safe = sanitize_namespace(session_id)
        value = mode if mode in INTERCEPT_MODES else INTERCEPT_DISABLED
        async with self._lock:
            self._modes[safe] = value
        await self.publish(
            "intercept_mode_changed",
            {"session_id": safe, "mode": value},
        )
        return value

    async def get_mode(self, session_id: str) -> str:
        safe = sanitize_namespace(session_id)
        async with self._lock:
            return self._modes.get(safe, INTERCEPT_DISABLED)

    @staticmethod
    def mode_matches_stage(mode: str, stage: str) -> bool:
        if mode == INTERCEPT_DISABLED:
            return False
        if stage == INTERCEPT_STAGE_CALL:
            return mode in {INTERCEPT_CALL, INTERCEPT_CALL_AND_REPLY}
        if stage == INTERCEPT_STAGE_REPLY:
            return mode in {INTERCEPT_REPLY, INTERCEPT_CALL_AND_REPLY}
        return False

    async def create_pending(
        self,
        *,
        session_id: str,
        stage: str,
        tool_name: str,
        call_payload: dict[str, Any],
        tool_arguments: dict[str, Any],
        output_schema: dict[str, Any] | None = None,
        tool_response: dict[str, Any] | None = None,
    ) -> PendingIntercept:
        safe = sanitize_namespace(session_id)
        item = PendingIntercept(
            intercept_id=f"i-{uuid.uuid4().hex}",
            session_id=safe,
            stage=stage,
            tool_name=tool_name,
            created_monotonic=_now_ts(),
            timeout_seconds=self._timeout_seconds,
            call_payload=call_payload,
            tool_arguments=tool_arguments,
            output_schema=output_schema,
            tool_response=tool_response,
            decision_event=asyncio.Event(),
        )
        async with self._lock:
            self._pending[item.intercept_id] = item
        await self.publish("intercept_pending", item.as_public_dict())
        return item

    async def wait_for_decision(self, intercept_id: str) -> tuple[str, dict[str, Any] | None]:
        async with self._lock:
            item = self._pending.get(intercept_id)
        if item is None or item.decision_event is None:
            return ("timeout_forward", None)
        try:
            await asyncio.wait_for(item.decision_event.wait(), timeout=item.timeout_seconds)
        except asyncio.TimeoutError:
            await self._resolve(intercept_id, "timeout_forward", None)
            await self._drop_pending(intercept_id)
            return ("timeout_forward", None)
        async with self._lock:
            final = self._pending.get(intercept_id)
            if final is None:
                return ("timeout_forward", None)
            action = final.operator_action or "timeout_forward"
            payload = final.operator_payload
        await self._drop_pending(intercept_id)
        return (action, payload)

    async def resolve_from_operator(
        self,
        intercept_id: str,
        action: str,
        payload: dict[str, Any] | None,
    ) -> bool:
        async with self._lock:
            item = self._pending.get(intercept_id)
            if item is None or item.decision_event is None:
                return False
            item.operator_action = action
            item.operator_payload = payload
            item.decision_event.set()
        await self.publish(
            "intercept_resolved",
            {
                "intercept_id": intercept_id,
                "session_id": item.session_id,
                "action": action,
            },
        )
        return True

    async def _resolve(self, intercept_id: str, action: str, payload: dict[str, Any] | None) -> None:
        async with self._lock:
            item = self._pending.get(intercept_id)
            if item is None:
                return
            item.operator_action = action
            item.operator_payload = payload
            if item.decision_event is not None:
                item.decision_event.set()
        await self.publish(
            "intercept_resolved",
            {
                "intercept_id": intercept_id,
                "session_id": item.session_id,
                "action": action,
            },
        )

    async def _drop_pending(self, intercept_id: str) -> None:
        async with self._lock:
            self._pending.pop(intercept_id, None)

    async def get_pending_for_session(self, session_id: str) -> list[dict[str, Any]]:
        safe = sanitize_namespace(session_id)
        async with self._lock:
            items = [item.as_public_dict() for item in self._pending.values() if item.session_id == safe]
        return sorted(items, key=lambda item: item["created_monotonic"])

    async def get_pending_by_id(self, intercept_id: str) -> dict[str, Any] | None:
        async with self._lock:
            item = self._pending.get(intercept_id)
            return None if item is None else item.as_public_dict()

    async def list_all_pending(self) -> list[dict[str, Any]]:
        async with self._lock:
            items = [item.as_public_dict() for item in self._pending.values()]
        return sorted(items, key=lambda item: item["created_monotonic"])

    async def register_active_session(self, session_id: str, session_obj: Any) -> None:
        safe = sanitize_namespace(session_id)
        ref_id = f"{id(session_obj)}"
        target = ActiveSessionTarget(
            session_id=safe,
            session_obj=session_obj,
            last_seen_epoch=__import__("time").time(),
            ref_id=ref_id,
        )
        mode_before = await self.is_session_send_available(safe)
        async with self._lock:
            bucket = self._active_targets.setdefault(safe, {})
            bucket[ref_id] = target
        mode_after = await self.is_session_send_available(safe)
        if mode_before != mode_after:
            await self.publish(
                "session_send_availability_changed",
                {"session_id": safe, "can_send_message": mode_after},
            )

    async def get_latest_active_session(self, session_id: str) -> ActiveSessionTarget | None:
        safe = sanitize_namespace(session_id)
        async with self._lock:
            bucket = self._active_targets.get(safe, {})
            if not bucket:
                return None
            return max(bucket.values(), key=lambda item: item.last_seen_epoch)

    async def get_connected_client_count(self, session_id: str) -> int:
        safe = sanitize_namespace(session_id)
        async with self._lock:
            return len(self._active_targets.get(safe, {}))

    async def is_session_send_available(self, session_id: str) -> bool:
        safe = sanitize_namespace(session_id)
        async with self._lock:
            return bool(self._active_targets.get(safe))

    async def send_log_message_to_session(
        self,
        session_id: str,
        *,
        level: str,
        data: Any,
        logger_name: str,
    ) -> tuple[bool, str]:
        safe = sanitize_namespace(session_id)
        target = await self.get_latest_active_session(safe)
        if target is None:
            return (False, "session_offline")
        session_obj = target.session_obj
        try:
            await session_obj.send_log_message(level=level, data=data, logger=logger_name)
            await self.register_active_session(safe, session_obj)
            await self.publish(
                "session_message_sent",
                {"session_id": safe},
            )
            return (True, "sent")
        except Exception:
            async with self._lock:
                bucket = self._active_targets.get(safe, {})
                bucket.pop(target.ref_id, None)
                if not bucket:
                    self._active_targets.pop(safe, None)
            await self.publish(
                "session_send_availability_changed",
                {"session_id": safe, "can_send_message": False},
            )
            return (False, "session_offline")

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        event = {"type": event_type, "data": data}
        async with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue

    async def get_session_graph_svg(self, session_id: str) -> str | None:
        safe = sanitize_namespace(session_id)
        try:
            return build_session_graph_svg(safe)
        except Exception:
            return None

    async def get_graphs_by_session(self, session_ids: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for session_id in session_ids:
            if isinstance(session_id, str) and session_id:
                svg = await self.get_session_graph_svg(session_id)
                if isinstance(svg, str) and svg.strip():
                    out[session_id] = svg
        return out

    async def publish_session_graph_updated(self, session_id: str) -> None:
        safe = sanitize_namespace(session_id)
        svg = await self.get_session_graph_svg(safe)
        if not isinstance(svg, str) or not svg.strip():
            return
        await self.publish(
            "session_graph_updated",
            {
                "session_id": safe,
                "svg": svg,
            },
        )

    async def list_sessions_summary(self) -> list[dict[str, Any]]:
        sessions = set()
        async with self._lock:
            sessions.update(self._active_targets.keys())
        for path in STORE_DIR.glob("*/session.json"):
            if path.parent.name:
                sessions.add(path.parent.name)
        for path in STORE_DIR.glob("*_log.jsonl"):
            stem = path.name[: -len("_log.jsonl")]
            if stem:
                sessions.add(stem)
        now_epoch = __import__("time").time()
        rows: list[dict[str, Any]] = []
        for session_id in sorted(sessions):
            logs = read_recent_logs(session_id, limit=5000)
            last_activity_iso = logs[-1]["time"] if logs else None
            last_epoch = _parse_iso_to_epoch(last_activity_iso) if isinstance(last_activity_iso, str) else None
            calls_last_hour = 0
            if logs:
                hour_ago = now_epoch - 3600
                for item in logs:
                    stamp = _parse_iso_to_epoch(item.get("time"))
                    if stamp is not None and stamp >= hour_ago:
                        calls_last_hour += 1
            mode = await self.get_mode(session_id)
            pending = await self.get_pending_for_session(session_id)
            active = await self.get_latest_active_session(session_id)
            connected_clients = await self.get_connected_client_count(session_id)
            rows.append(
                {
                    "session_id": session_id,
                    "calls_per_second": (calls_last_hour / 3600.0) if calls_last_hour else 0.0,
                    "calls_last_hour": calls_last_hour,
                    "last_activity_iso": last_activity_iso,
                    "last_activity_seconds_ago": (now_epoch - last_epoch) if last_epoch is not None else None,
                    "intercept_mode": mode,
                    "pending_intercepts": pending,
                    "can_send_message": active is not None,
                    "agent_last_seen_epoch": active.last_seen_epoch if active is not None else None,
                    "connected_clients": connected_clients,
                }
            )
        rows.sort(key=lambda row: row["last_activity_seconds_ago"] if row["last_activity_seconds_ago"] is not None else 10**12)
        return rows


def _parse_iso_to_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    import datetime as dt

    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _session_log_path(session_id: str) -> Path:
    return STORE_DIR / f"{_safe_session_log_name(session_id)}_log.jsonl"


def read_recent_logs(session_id: str, *, limit: int = 15) -> list[dict[str, Any]]:
    path = _session_log_path(session_id)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = lines[-max(1, limit) :] if limit > 0 else lines
    out: list[dict[str, Any]] = []
    for line in selected:
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                out.append(parsed)
        except Exception:
            continue
    return out


def bootstrap_resource_files(base_dir: Path) -> list[str]:
    if not base_dir.exists():
        return []
    return sorted([path.name for path in base_dir.iterdir() if path.is_file()])


def new_event_payload_for_log(session_id: str, log_entry: dict[str, Any]) -> dict[str, Any]:
    now_epoch = __import__("time").time()
    return {
        "session_id": sanitize_namespace(session_id),
        "time": log_entry.get("time") or _iso_from_epoch(now_epoch),
        "entry": log_entry,
    }


SUPERVISOR = SupervisorCoordinator()
