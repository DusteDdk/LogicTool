from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import STORE_DIR
from .session_graph import build_session_graph_table
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
STALE_SESSION_REMOVE_SECONDS = float(os.getenv("LOGIC_SUPERVISOR_STALE_SESSION_REMOVE_SEC", "7200"))
STALE_SIDECAR_REMOVE_SECONDS = float(os.getenv("LOGIC_SUPERVISOR_STALE_SIDECAR_REMOVE_SEC", "3600"))
SIDECAR_COMMAND_TIMEOUT_SECONDS = float(os.getenv("LOGIC_SUPERVISOR_SIDECAR_COMMAND_TIMEOUT_SEC", "30"))


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


@dataclass
class PendingSidecarCommand:
    command_id: str
    instance_id: str
    command: str
    created_monotonic: float
    timeout_seconds: float
    decision_event: asyncio.Event | None = None
    result: dict[str, Any] | None = None


@dataclass
class SidecarConnection:
    instance_id: str
    connection: Any
    workdir: str
    local: str
    session_id: str | None
    pid: int | None
    remote: str
    tool_url: str | None
    connected: bool
    connected_at_epoch: float
    last_seen_epoch: float
    disconnected_at_epoch: float | None = None
    last_command_id: str | None = None
    last_command_name: str | None = None
    last_command_ok: bool | None = None
    last_command_summary: str | None = None
    last_command_epoch: float | None = None

    def state(self) -> str:
        if not self.connected:
            return "Disconnected"
        if not self.session_id:
            return "Idle"
        safe_session = sanitize_namespace(self.session_id)
        if (STORE_DIR / safe_session).exists():
            return "Attached"
        return "Tentative"

    def as_public_dict(self) -> dict[str, Any]:
        now_epoch = __import__("time").time()
        disconnected_age = (
            (now_epoch - self.disconnected_at_epoch)
            if isinstance(self.disconnected_at_epoch, (int, float))
            else None
        )
        return {
            "instance_id": self.instance_id,
            "workdir": self.workdir,
            "local": self.local,
            "session_id": self.session_id,
            "pid": self.pid,
            "remote": self.remote,
            "tool_url": self.tool_url,
            "connected": self.connected,
            "state": self.state(),
            "connected_at_epoch": self.connected_at_epoch,
            "last_seen_epoch": self.last_seen_epoch,
            "disconnected_at_epoch": self.disconnected_at_epoch,
            "disconnected_seconds_ago": disconnected_age,
            "last_command_id": self.last_command_id,
            "last_command_name": self.last_command_name,
            "last_command_ok": self.last_command_ok,
            "last_command_summary": self.last_command_summary,
            "last_command_epoch": self.last_command_epoch,
        }


class SupervisorCoordinator:
    def __init__(self) -> None:
        self._modes: dict[str, str] = {}
        self._pending: dict[str, PendingIntercept] = {}
        self._active_targets: dict[str, dict[str, ActiveSessionTarget]] = {}
        self._sidecars: dict[str, SidecarConnection] = {}
        self._pending_sidecar_commands: dict[str, PendingSidecarCommand] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._timeout_seconds = float(os.getenv("LOGIC_SUPERVISOR_INTERCEPT_TIMEOUT_SEC", "600"))
        self._sidecar_command_timeout_seconds = SIDECAR_COMMAND_TIMEOUT_SECONDS

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

    @staticmethod
    def _normalize_sidecar_session(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        clean = value.strip()
        if not clean:
            return None
        return sanitize_namespace(clean)

    @staticmethod
    def _normalize_sidecar_text(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _normalize_optional_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip():
            try:
                return int(value.strip())
            except Exception:
                return None
        return None

    def _prune_disconnected_sidecars_locked(self, now_epoch: float | None = None) -> None:
        now_value = __import__("time").time() if now_epoch is None else float(now_epoch)
        remove_ids: list[str] = []
        for instance_id, sidecar in self._sidecars.items():
            if sidecar.connected:
                continue
            if not isinstance(sidecar.disconnected_at_epoch, (int, float)):
                continue
            if (now_value - sidecar.disconnected_at_epoch) > STALE_SIDECAR_REMOVE_SECONDS:
                remove_ids.append(instance_id)
        for instance_id in remove_ids:
            self._sidecars.pop(instance_id, None)
            stale_command_ids = [
                command_id
                for command_id, pending in self._pending_sidecar_commands.items()
                if pending.instance_id == instance_id
            ]
            for command_id in stale_command_ids:
                pending = self._pending_sidecar_commands.pop(command_id, None)
                if pending is not None and pending.decision_event is not None and not pending.decision_event.is_set():
                    pending.decision_event.set()

    async def register_sidecar_connection(
        self,
        *,
        instance_id: str,
        connection: Any,
        workdir: Any,
        local: Any,
        session_id: Any,
        pid: Any,
        remote: Any,
        tool_url: Any,
    ) -> dict[str, Any]:
        clean_instance = instance_id.strip() if isinstance(instance_id, str) else ""
        if not clean_instance:
            clean_instance = f"sidecar-{uuid.uuid4().hex}"
        now_epoch = __import__("time").time()
        public_payload: dict[str, Any] | None = None
        async with self._lock:
            self._prune_disconnected_sidecars_locked(now_epoch)
            existing = self._sidecars.get(clean_instance)
            connected_at = now_epoch
            last_command_id = None
            last_command_name = None
            last_command_ok = None
            last_command_summary = None
            last_command_epoch = None
            if existing is not None:
                connected_at = existing.connected_at_epoch if existing.connected else now_epoch
                last_command_id = existing.last_command_id
                last_command_name = existing.last_command_name
                last_command_ok = existing.last_command_ok
                last_command_summary = existing.last_command_summary
                last_command_epoch = existing.last_command_epoch
            item = SidecarConnection(
                instance_id=clean_instance,
                connection=connection,
                workdir=self._normalize_sidecar_text(workdir),
                local=self._normalize_sidecar_text(local),
                session_id=self._normalize_sidecar_session(session_id),
                pid=self._normalize_optional_int(pid),
                remote=self._normalize_sidecar_text(remote),
                tool_url=self._normalize_sidecar_text(tool_url) or None,
                connected=True,
                connected_at_epoch=connected_at,
                last_seen_epoch=now_epoch,
                disconnected_at_epoch=None,
                last_command_id=last_command_id,
                last_command_name=last_command_name,
                last_command_ok=last_command_ok,
                last_command_summary=last_command_summary,
                last_command_epoch=last_command_epoch,
            )
            self._sidecars[clean_instance] = item
            public_payload = item.as_public_dict()
        if public_payload is not None:
            await self.publish("sidecar_updated", public_payload)
        return public_payload or {"instance_id": clean_instance}

    async def update_sidecar_connection(
        self,
        instance_id: str,
        *,
        workdir: Any | None = None,
        local: Any | None = None,
        session_id: Any | None = None,
        pid: Any | None = None,
        remote: Any | None = None,
        tool_url: Any | None = None,
    ) -> dict[str, Any] | None:
        clean_instance = instance_id.strip() if isinstance(instance_id, str) else ""
        if not clean_instance:
            return None
        now_epoch = __import__("time").time()
        public_payload: dict[str, Any] | None = None
        async with self._lock:
            self._prune_disconnected_sidecars_locked(now_epoch)
            item = self._sidecars.get(clean_instance)
            if item is None:
                return None
            item.last_seen_epoch = now_epoch
            if workdir is not None:
                item.workdir = self._normalize_sidecar_text(workdir)
            if local is not None:
                item.local = self._normalize_sidecar_text(local)
            if session_id is not None:
                item.session_id = self._normalize_sidecar_session(session_id)
            if pid is not None:
                item.pid = self._normalize_optional_int(pid)
            if remote is not None:
                item.remote = self._normalize_sidecar_text(remote)
            if tool_url is not None:
                clean_url = self._normalize_sidecar_text(tool_url)
                item.tool_url = clean_url or None
            public_payload = item.as_public_dict()
        if public_payload is not None:
            await self.publish("sidecar_updated", public_payload)
        return public_payload

    async def mark_sidecar_disconnected(self, instance_id: str) -> bool:
        clean_instance = instance_id.strip() if isinstance(instance_id, str) else ""
        if not clean_instance:
            return False
        now_epoch = __import__("time").time()
        public_payload: dict[str, Any] | None = None
        async with self._lock:
            item = self._sidecars.get(clean_instance)
            if item is None:
                return False
            item.connected = False
            item.connection = None
            item.last_seen_epoch = now_epoch
            item.disconnected_at_epoch = now_epoch
            public_payload = item.as_public_dict()
            self._prune_disconnected_sidecars_locked(now_epoch)
        if public_payload is not None:
            await self.publish("sidecar_updated", public_payload)
        return True

    async def list_sidecars_summary(self) -> list[dict[str, Any]]:
        async with self._lock:
            self._prune_disconnected_sidecars_locked()
            rows = [item.as_public_dict() for item in self._sidecars.values()]
        rows.sort(
            key=lambda row: (
                0 if row.get("connected") else 1,
                str(row.get("state") or ""),
                str(row.get("workdir") or ""),
                str(row.get("instance_id") or ""),
            )
        )
        return rows

    async def send_command_to_sidecar(
        self,
        instance_id: str,
        *,
        command: str,
        arguments: dict[str, Any] | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        clean_instance = instance_id.strip() if isinstance(instance_id, str) else ""
        if not clean_instance:
            return (False, "sidecar_not_found", None)
        clean_command = command.strip() if isinstance(command, str) else ""
        if not clean_command:
            return (False, "sidecar_command_invalid", None)

        payload_args = arguments if isinstance(arguments, dict) else {}
        now_epoch = __import__("time").time()
        command_id = f"sc-{uuid.uuid4().hex}"
        queue_item = PendingSidecarCommand(
            command_id=command_id,
            instance_id=clean_instance,
            command=clean_command,
            created_monotonic=_now_ts(),
            timeout_seconds=self._sidecar_command_timeout_seconds,
            decision_event=asyncio.Event(),
        )

        connection = None
        sidecar_payload: dict[str, Any] | None = None
        async with self._lock:
            self._prune_disconnected_sidecars_locked(now_epoch)
            sidecar = self._sidecars.get(clean_instance)
            if sidecar is None:
                return (False, "sidecar_not_found", None)
            if not sidecar.connected or sidecar.connection is None:
                return (False, "sidecar_offline", None)
            connection = sidecar.connection
            self._pending_sidecar_commands[command_id] = queue_item
            sidecar.last_command_id = command_id
            sidecar.last_command_name = clean_command
            sidecar.last_command_ok = None
            sidecar.last_command_summary = "pending"
            sidecar.last_command_epoch = now_epoch
            sidecar_payload = sidecar.as_public_dict()

        if sidecar_payload is not None:
            await self.publish("sidecar_updated", sidecar_payload)

        try:
            await connection.send_json(
                {
                    "type": "command",
                    "command_id": command_id,
                    "command": clean_command,
                    "args": payload_args,
                }
            )
        except Exception:
            async with self._lock:
                self._pending_sidecar_commands.pop(command_id, None)
            await self.mark_sidecar_disconnected(clean_instance)
            return (False, "sidecar_offline", None)

        if queue_item.decision_event is None:
            return (False, "sidecar_command_timeout", None)
        try:
            await asyncio.wait_for(queue_item.decision_event.wait(), timeout=queue_item.timeout_seconds)
        except asyncio.TimeoutError:
            timed_out_payload: dict[str, Any] | None = None
            async with self._lock:
                self._pending_sidecar_commands.pop(command_id, None)
                sidecar = self._sidecars.get(clean_instance)
                if sidecar is not None and sidecar.last_command_id == command_id:
                    sidecar.last_command_ok = False
                    sidecar.last_command_summary = "timeout"
                    sidecar.last_command_epoch = __import__("time").time()
                    timed_out_payload = sidecar.as_public_dict()
            if timed_out_payload is not None:
                await self.publish("sidecar_updated", timed_out_payload)
            return (False, "sidecar_command_timeout", None)

        async with self._lock:
            resolved = self._pending_sidecar_commands.pop(command_id, None)
            result = resolved.result if resolved is not None else None
        if isinstance(result, dict):
            return (True, "ok", result)
        return (True, "ok", {})

    async def resolve_sidecar_command(
        self,
        instance_id: str,
        command_id: str,
        payload: dict[str, Any],
    ) -> bool:
        clean_instance = instance_id.strip() if isinstance(instance_id, str) else ""
        clean_command_id = command_id.strip() if isinstance(command_id, str) else ""
        if not clean_instance or not clean_command_id:
            return False

        publish_payload: dict[str, Any] | None = None
        matched = False
        now_epoch = __import__("time").time()
        async with self._lock:
            sidecar = self._sidecars.get(clean_instance)
            if sidecar is not None:
                sidecar.last_seen_epoch = now_epoch
                sidecar.last_command_id = clean_command_id
                command_name = payload.get("command")
                if isinstance(command_name, str) and command_name:
                    sidecar.last_command_name = command_name
                if isinstance(payload.get("ok"), bool):
                    sidecar.last_command_ok = payload["ok"]
                summary = payload.get("message")
                if not isinstance(summary, str) or not summary.strip():
                    if isinstance(payload.get("error"), str) and payload["error"]:
                        summary = payload["error"]
                    elif sidecar.last_command_ok is True:
                        summary = "ok"
                    elif sidecar.last_command_ok is False:
                        summary = "failed"
                    else:
                        summary = ""
                sidecar.last_command_summary = summary.strip() if isinstance(summary, str) else ""
                sidecar.last_command_epoch = now_epoch
                publish_payload = sidecar.as_public_dict()

            pending = self._pending_sidecar_commands.get(clean_command_id)
            if pending is not None and pending.instance_id == clean_instance and pending.decision_event is not None:
                pending.result = payload
                pending.decision_event.set()
                matched = True

        if publish_payload is not None:
            await self.publish("sidecar_updated", publish_payload)
        return matched

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

    async def get_session_graph_table(self, session_id: str) -> dict[str, Any] | None:
        safe = sanitize_namespace(session_id)
        try:
            return build_session_graph_table(safe)
        except Exception:
            return None

    async def get_graphs_by_session(self, session_ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for session_id in session_ids:
            if isinstance(session_id, str) and session_id:
                table = await self.get_session_graph_table(session_id)
                if isinstance(table, dict):
                    out[session_id] = table
        return out

    async def publish_session_graph_updated(self, session_id: str) -> None:
        safe = sanitize_namespace(session_id)
        table = await self.get_session_graph_table(safe)
        if not isinstance(table, dict):
            return
        await self.publish(
            "session_graph_updated",
            {
                "session_id": safe,
                "table": table,
            },
        )

    async def list_sessions_summary(self) -> list[dict[str, Any]]:
        sessions = set()
        async with self._lock:
            sessions.update(self._active_targets.keys())
        if STORE_DIR.exists():
            for path in STORE_DIR.iterdir():
                if path.is_dir() and path.name:
                    sessions.add(sanitize_namespace(path.name))
        now_epoch = __import__("time").time()
        rows: list[dict[str, Any]] = []
        for session_id in sorted(sessions, key=_natural_sort_key):
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
            session_age_seconds = _session_age_seconds(
                session_id,
                last_activity_epoch=last_epoch,
                now_epoch=now_epoch,
            )
            can_remove_session = session_age_seconds is not None
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
                    "session_age_seconds_ago": session_age_seconds,
                    "can_remove_session": can_remove_session,
                }
            )
        return rows

    async def delete_session_data(self, session_id: str) -> tuple[bool, str]:
        safe = sanitize_namespace(session_id)
        now_epoch = __import__("time").time()
        session_age_seconds = _session_age_seconds(safe, now_epoch=now_epoch)
        if session_age_seconds is None:
            return (False, "session_not_found")

        log_path = _session_log_path(safe)
        session_dir = _session_dir_path(safe)
        removed_any = False
        try:
            if log_path.exists():
                log_path.unlink()
                removed_any = True
            if session_dir.exists():
                shutil.rmtree(session_dir)
                removed_any = True
        except Exception:
            return (False, "session_delete_failed")
        if not removed_any:
            return (False, "session_not_found")

        async with self._lock:
            self._modes.pop(safe, None)
            self._active_targets.pop(safe, None)
            pending_ids = [item_id for item_id, item in self._pending.items() if item.session_id == safe]
            for item_id in pending_ids:
                item = self._pending.pop(item_id, None)
                if item is not None and item.decision_event is not None and not item.decision_event.is_set():
                    item.decision_event.set()
        await self.publish("session_removed", {"session_id": safe})
        return (True, "removed")


def _parse_iso_to_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    import datetime as dt

    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _natural_sort_key(value: str) -> tuple[tuple[int, Any], ...]:
    parts = re.split(r"(\d+)", value)
    key: list[tuple[int, Any]] = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part.lower()))
    return tuple(key)


def _session_log_path(session_id: str) -> Path:
    safe = sanitize_namespace(session_id)
    return STORE_DIR / safe / "log.jsonl"


def _session_dir_path(session_id: str) -> Path:
    safe = sanitize_namespace(session_id)
    return STORE_DIR / safe


def _session_latest_activity_epoch(session_id: str, *, last_activity_epoch: float | None = None) -> float | None:
    safe = sanitize_namespace(session_id)
    epochs: list[float] = []
    if isinstance(last_activity_epoch, (int, float)):
        epochs.append(float(last_activity_epoch))

    log_path = _session_log_path(safe)
    if log_path.exists():
        try:
            epochs.append(float(log_path.stat().st_mtime))
        except Exception:
            pass
        if last_activity_epoch is None:
            latest_log = read_recent_logs(safe, limit=1)
            if latest_log:
                latest_log_epoch = _parse_iso_to_epoch(latest_log[-1].get("time"))
                if isinstance(latest_log_epoch, float):
                    epochs.append(latest_log_epoch)

    session_dir = _session_dir_path(safe)
    if session_dir.exists():
        try:
            epochs.append(float(session_dir.stat().st_mtime))
        except Exception:
            pass
        session_file = session_dir / "session.json"
        if session_file.exists():
            try:
                epochs.append(float(session_file.stat().st_mtime))
            except Exception:
                pass

    if not epochs:
        return None
    return max(epochs)


def _session_age_seconds(
    session_id: str,
    *,
    last_activity_epoch: float | None = None,
    now_epoch: float | None = None,
) -> float | None:
    latest_epoch = _session_latest_activity_epoch(session_id, last_activity_epoch=last_activity_epoch)
    if latest_epoch is None:
        return None
    now_value = __import__("time").time() if now_epoch is None else now_epoch
    return max(0.0, now_value - latest_epoch)


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
