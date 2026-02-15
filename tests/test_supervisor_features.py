from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
import uuid

from starlette.testclient import TestClient

from logic_mcp.audit_log import append_tool_log
from logic_mcp.engine import call_tool
from logic_mcp.engine import get_engine
from logic_mcp.engine import server
from logic_mcp.paths import PROJECT_ROOT
from logic_mcp.paths import STORE_DIR
from logic_mcp.store import sanitize_namespace
from logic_mcp.supervisor import INTERCEPT_CALL
from logic_mcp.supervisor import INTERCEPT_DISABLED
from logic_mcp.supervisor import INTERCEPT_REPLY
from logic_mcp.supervisor import SUPERVISOR
from logic_mcp.transport_http import create_http_app


class FakeSession:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_log_message(self, *, level: str, data: object, logger: str | None = None) -> None:
        self.sent.append({"level": level, "data": data, "logger": logger})


class FakeSidecarSocket:
    def __init__(self, responder=None) -> None:
        self.sent: list[dict] = []
        self._responder = responder

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)
        if self._responder is None:
            return
        outcome = self._responder(payload)
        if asyncio.iscoroutine(outcome):
            await outcome


class SupervisorFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session_id = f"ut-sup-{uuid.uuid4().hex[:8]}"
        self.safe_session_id = sanitize_namespace(self.session_id)
        self.session_dir = STORE_DIR / self.safe_session_id
        self.log_file = self.session_dir / "log.jsonl"
        self._extra_session_dirs: list = []
        self._extra_log_files: list = []
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.app = create_http_app(server)
        self.client = TestClient(self.app)
        asyncio.run(self._reset_supervisor_state())

    def tearDown(self) -> None:
        self.client.close()
        if self.log_file.exists():
            self.log_file.unlink()
        if self.session_dir.exists():
            for path in self.session_dir.glob("*"):
                if path.is_file():
                    path.unlink()
            self.session_dir.rmdir()
        for session_dir in self._extra_session_dirs:
            if session_dir.exists():
                for path in session_dir.glob("*"):
                    if path.is_file():
                        path.unlink()
                session_dir.rmdir()
        for log_file in self._extra_log_files:
            if log_file.exists():
                log_file.unlink()
        asyncio.run(self._reset_supervisor_state())

    async def _reset_supervisor_state(self) -> None:
        async with SUPERVISOR._lock:
            for item in SUPERVISOR._pending.values():
                if item.decision_event is not None and not item.decision_event.is_set():
                    item.decision_event.set()
            for item in SUPERVISOR._pending_sidecar_commands.values():
                if item.decision_event is not None and not item.decision_event.is_set():
                    item.decision_event.set()
            SUPERVISOR._pending.clear()
            SUPERVISOR._pending_sidecar_commands.clear()
            SUPERVISOR._modes.clear()
            SUPERVISOR._active_targets.clear()
            SUPERVISOR._sidecars.clear()
            SUPERVISOR._subscribers.clear()
            SUPERVISOR._timeout_seconds = 600.0
            SUPERVISOR._sidecar_command_timeout_seconds = 30.0

    def _seed_log(self) -> None:
        entry = {
            "time": "2026-02-13T10:00:00.000Z",
            "call": {"name": "logic_list", "arguments": {}},
            "response": {"ok": True, "result": {"items": []}},
            "request_duration_ms": 6,
        }
        self.log_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    def _add_empty_session_dir(self, session_id: str) -> str:
        safe = sanitize_namespace(session_id)
        path = STORE_DIR / safe
        path.mkdir(parents=True, exist_ok=True)
        self._extra_session_dirs.append(path)
        return safe

    @staticmethod
    def _log_entry_for_time(iso_time: str) -> dict:
        return {
            "time": iso_time,
            "call": {"name": "logic_list", "arguments": {}},
            "response": {"ok": True, "result": {"items": []}},
            "request_duration_ms": 6,
        }

    def test_append_tool_log_includes_request_duration_ms(self) -> None:
        entry = append_tool_log(
            self.safe_session_id,
            {"name": "logic_list", "arguments": {}},
            {"ok": True, "result": {"items": []}},
            request_duration_ms=27,
        )
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.get("request_duration_ms"), 27)
        lines = self.log_file.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines)
        payload = json.loads(lines[-1])
        self.assertEqual(payload.get("request_duration_ms"), 27)

    def test_bootstrap_routes(self) -> None:
        response = self.client.get("/agents/bootstrap")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("bootstrap_urls", payload)
        urls = payload["bootstrap_urls"]
        self.assertTrue(urls)
        first = urls[0]
        self.assertTrue(first.startswith("http://"))
        path_start = first.find("/agents/bootstrap/resources/")
        self.assertNotEqual(path_start, -1)
        resource_path = first[path_start:]
        file_response = self.client.get(resource_path)
        self.assertEqual(file_response.status_code, 200)
        self.assertGreater(len(file_response.text), 0)

    def test_sidecar_bootstrap_routes(self) -> None:
        response = self.client.get("/agents/bootstrap/sidecar/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("artifact_urls", payload)
        urls = payload["artifact_urls"]
        self.assertTrue(urls)
        first = urls[0]
        self.assertTrue(first.startswith("http://"))
        path_start = first.find("/agents/bootstrap/sidecar/")
        self.assertNotEqual(path_start, -1)
        resource_path = first[path_start:]
        artifact_response = self.client.get(resource_path)
        self.assertEqual(artifact_response.status_code, 200)
        self.assertIn("LogiCar startup...", artifact_response.text)
        self.assertIn('LOCAL_VENV_DIR_NAME = ".venv_logicar"', artifact_response.text)
        self.assertIn("def ensure_local_venv_runtime(", artifact_response.text)

    def test_supervisor_page_and_session_api(self) -> None:
        page = self.client.get("/supervisor")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Supervisor Dashboard", page.text)

        self._seed_log()
        sessions = self.client.get("/supervisor/api/sessions")
        self.assertEqual(sessions.status_code, 200)
        body = sessions.json()
        self.assertTrue(body.get("ok"))
        session_ids = {row["session_id"] for row in body.get("sessions", [])}
        self.assertIn(self.safe_session_id, session_ids)

    def test_sessions_include_inactive_directories_and_use_natural_order(self) -> None:
        prefix = f"ut-order-{uuid.uuid4().hex[:6]}-"
        sid_1 = self._add_empty_session_dir(prefix + "1")
        sid_2 = self._add_empty_session_dir(prefix + "2")
        sid_10 = self._add_empty_session_dir(prefix + "10")
        payload = self.client.get("/supervisor/api/sessions").json()
        rows = payload.get("sessions", [])
        ids = [row.get("session_id") for row in rows]
        self.assertIn(sid_1, ids)
        self.assertIn(sid_2, ids)
        self.assertIn(sid_10, ids)
        self.assertLess(ids.index(sid_1), ids.index(sid_2))
        self.assertLess(ids.index(sid_2), ids.index(sid_10))
        row_1 = next((row for row in rows if row.get("session_id") == sid_1), None)
        self.assertIsNotNone(row_1)
        assert row_1 is not None
        self.assertIsNone(row_1.get("last_activity_iso"))
        self.assertEqual(row_1.get("calls_last_hour"), 0)

    def test_snapshot_includes_graph_table_for_inactive_session(self) -> None:
        inactive_session = self._add_empty_session_dir(f"ut-graph-{uuid.uuid4().hex[:8]}")
        with self.client.websocket_connect("/supervisor/ws") as socket:
            first = socket.receive_json()
            self.assertEqual(first.get("type"), "snapshot")
            data = first.get("data", {})
            session_rows = data.get("sessions", [])
            session_ids = [row.get("session_id") for row in session_rows if isinstance(row, dict)]
            self.assertIn(inactive_session, session_ids)
            graphs = data.get("graphs_by_session", {})
            self.assertIn(inactive_session, graphs)
            table = graphs[inactive_session]
            self.assertIsInstance(table, dict)
            self.assertIn("rows", table)
            self.assertIsInstance(table["rows"], list)

    def test_frontend_assets_keep_log_and_icons_visible_by_default(self) -> None:
        app_js_path = PROJECT_ROOT / "logic_mcp" / "resources" / "supervisor" / "app.js"
        styles_path = PROJECT_ROOT / "logic_mcp" / "resources" / "supervisor" / "styles.css"
        app_js = app_js_path.read_text(encoding="utf-8")
        styles = styles_path.read_text(encoding="utf-8")

        self.assertIn('headline.addEventListener("click", function () {', app_js)
        self.assertIn('body.className = expanded ? "session-body open" : "session-body";', app_js)
        self.assertIn('body.hidden = !expanded;', app_js)
        self.assertIn('const headline = document.createElement("div");', app_js)
        self.assertIn("view.sessions.cardsById", app_js)
        self.assertIn("view.sidecars.cardsById", app_js)

        sessions_render_block = app_js.split("function renderSessionsFromState() {", 1)[1].split(
            "function applySnapshot", 1
        )[0]
        self.assertNotIn("sessionsEl.innerHTML", sessions_render_block)
        self.assertIn("root.list.appendChild(refs.card);", sessions_render_block)
        self.assertIn("buildMessageComposeCard(sessionId)", app_js)
        self.assertIn("refs.compose.setOnline(!!session.can_send_message);", app_js)

        sidecars_render_block = app_js.split("function renderSidecarsFromState() {", 1)[1].split(
            "function ensureSessionsRoot", 1
        )[0]
        self.assertNotIn("sidecarsEl.innerHTML", sidecars_render_block)
        self.assertIn("root.list.appendChild(refs.card);", sidecars_render_block)

        log_panel_block = app_js.split("function buildLogPanel(sessionId, logs) {", 1)[1].split(
            "function itemTypeLabel", 1
        )[0]
        self.assertIn('document.createElement("div")', log_panel_block)
        self.assertNotIn('document.createElement("details")', log_panel_block)
        self.assertNotIn('title.textContent = "Logs";', log_panel_block)
        self.assertIn('pauseBtn.textContent = control.paused ? "Resume" : "Pause";', log_panel_block)
        self.assertIn("const parts = buildLogParts(entry);", log_panel_block)
        self.assertIn("appendDisplayIdentifier(line, part);", log_panel_block)
        self.assertIn("details.hidden = !details.hidden;", log_panel_block)
        self.assertIn('control.buffer = trimLogEntries(state.logsBySession[sessionId]);', app_js)
        self.assertIn('control.buffer = [];', app_js)

        self.assertIn("const parts = [operation, tool, shortName, language, itemId, duration, toolCallResult];", app_js)
        self.assertIn("return parts.join(\" \");", app_js)
        self.assertIn('if (value === "no-air-control") {', app_js)
        self.assertIn('line.className = "graph-relation-line relation-open mono";', app_js)
        self.assertIn('openContentModal(relatedRow, { originRow: originRow, relationLabel: relationLabel });', app_js)
        self.assertIn('"origin: " + originId + " (" + originType + originSuffix + ")",', app_js)
        self.assertIn('"references_outgoing: " + (outgoingRefs.length ? outgoingRefs.join(", ") : "[none]"),', app_js)
        self.assertIn('"references_incoming: " + (incomingRefs.length ? incomingRefs.join(", ") : "[none]"),', app_js)
        self.assertNotIn("formatReplyLogLine", app_js)

        self.assertIn("white-space: pre;", styles)
        self.assertIn("overflow-wrap: normal;", styles)
        self.assertNotIn("white-space: pre-wrap;", styles)
        self.assertIn("log-controls", styles)
        self.assertIn("log-line-clickable", styles)
        self.assertIn("content-modal-overlay", styles)
        self.assertIn(".content-modal-overlay[hidden]", styles)
        self.assertIn(".relation-open", styles)
        self.assertIn('resetSessionData(session.session_id, true)', app_js)
        self.assertIn('resetButton.textContent = "Reset session";', app_js)
        self.assertIn('removeSessionData(session.session_id)', app_js)
        self.assertIn('removeButton.textContent = "Remove session";', app_js)
        self.assertIn("session-actions", styles)
        self.assertIn("button.danger", styles)
        self.assertIn("button.warning", styles)

    def test_remove_session_api_deletes_stale_offline_session_data(self) -> None:
        safe = self._add_empty_session_dir(f"ut-remove-{uuid.uuid4().hex[:8]}")
        session_file = STORE_DIR / safe / "session.json"
        session_file.write_text("{}", encoding="utf-8")
        stale_iso = "2026-02-13T10:00:00.000Z"
        log_file = STORE_DIR / safe / "log.jsonl"
        log_file.write_text(json.dumps(self._log_entry_for_time(stale_iso)) + "\n", encoding="utf-8")
        self._extra_log_files.append(log_file)

        stale_epoch = time.time() - (3 * 3600)
        os.utime(STORE_DIR / safe, (stale_epoch, stale_epoch))
        os.utime(session_file, (stale_epoch, stale_epoch))
        os.utime(log_file, (stale_epoch, stale_epoch))

        sessions_before = self.client.get("/supervisor/api/sessions").json().get("sessions", [])
        row_before = next((row for row in sessions_before if row.get("session_id") == safe), None)
        self.assertIsNotNone(row_before)
        assert row_before is not None
        self.assertTrue(row_before.get("can_remove_session"))

        response = self.client.delete(f"/supervisor/api/sessions/{safe}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("deleted"))
        self.assertFalse((STORE_DIR / safe).exists())
        self.assertFalse(log_file.exists())

    def test_remove_session_api_removes_recent_session(self) -> None:
        safe = self._add_empty_session_dir(f"ut-recent-{uuid.uuid4().hex[:8]}")
        session_file = STORE_DIR / safe / "session.json"
        session_file.write_text("{}", encoding="utf-8")
        current_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log_file = STORE_DIR / safe / "log.jsonl"
        log_file.write_text(json.dumps(self._log_entry_for_time(current_iso)) + "\n", encoding="utf-8")
        self._extra_log_files.append(log_file)

        response = self.client.delete(f"/supervisor/api/sessions/{safe}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("deleted"))
        self.assertFalse((STORE_DIR / safe).exists())
        self.assertFalse(log_file.exists())

    def test_remove_session_api_removes_session_with_active_clients(self) -> None:
        safe = self._add_empty_session_dir(f"ut-active-{uuid.uuid4().hex[:8]}")
        session_file = STORE_DIR / safe / "session.json"
        session_file.write_text("{}", encoding="utf-8")
        stale_iso = "2026-02-13T10:00:00.000Z"
        log_file = STORE_DIR / safe / "log.jsonl"
        log_file.write_text(json.dumps(self._log_entry_for_time(stale_iso)) + "\n", encoding="utf-8")
        self._extra_log_files.append(log_file)

        stale_epoch = time.time() - (3 * 3600)
        os.utime(STORE_DIR / safe, (stale_epoch, stale_epoch))
        os.utime(session_file, (stale_epoch, stale_epoch))
        os.utime(log_file, (stale_epoch, stale_epoch))
        asyncio.run(SUPERVISOR.register_active_session(safe, FakeSession()))

        response = self.client.delete(f"/supervisor/api/sessions/{safe}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("deleted"))
        self.assertFalse((STORE_DIR / safe).exists())
        self.assertFalse(log_file.exists())

    def test_call_intercept_timeout_auto_forward(self) -> None:
        async def scenario() -> None:
            await SUPERVISOR.set_mode("default", INTERCEPT_CALL)
            SUPERVISOR._timeout_seconds = 0.05
            response = await call_tool("logic_list", {})
            self.assertTrue(response["ok"])
            pending = await SUPERVISOR.list_all_pending()
            self.assertEqual(pending, [])
            await SUPERVISOR.set_mode("default", INTERCEPT_DISABLED)

        asyncio.run(scenario())

    def test_call_intercept_override(self) -> None:
        async def scenario() -> None:
            await SUPERVISOR.set_mode("default", INTERCEPT_CALL)
            SUPERVISOR._timeout_seconds = 2.0
            task = asyncio.create_task(call_tool("logic_list", {}))
            await asyncio.sleep(0.05)
            pending = await SUPERVISOR.list_all_pending()
            self.assertTrue(pending)
            intercept_id = pending[0]["intercept_id"]
            await SUPERVISOR.resolve_from_operator(
                intercept_id,
                "override",
                {"response": {"ok": True, "result": {"items": [{"id": "manual"}]}}},
            )
            out = await task
            self.assertEqual(out["result"]["items"][0]["id"], "manual")
            await SUPERVISOR.set_mode("default", INTERCEPT_DISABLED)

        asyncio.run(scenario())

    def test_reply_intercept_send(self) -> None:
        async def scenario() -> None:
            await SUPERVISOR.set_mode("default", INTERCEPT_REPLY)
            SUPERVISOR._timeout_seconds = 2.0
            task = asyncio.create_task(call_tool("logic_list", {}))
            await asyncio.sleep(0.05)
            pending = await SUPERVISOR.list_all_pending()
            self.assertTrue(pending)
            intercept_id = pending[0]["intercept_id"]
            await SUPERVISOR.resolve_from_operator(
                intercept_id,
                "send",
                {"response": {"ok": True, "result": {"items": [{"id": "reply-manual"}]}}},
            )
            out = await task
            self.assertEqual(out["result"]["items"][0]["id"], "reply-manual")
            await SUPERVISOR.set_mode("default", INTERCEPT_DISABLED)

        asyncio.run(scenario())

    def test_mode_and_log_endpoints(self) -> None:
        self._seed_log()
        set_mode = self.client.post(
            f"/supervisor/api/sessions/{self.safe_session_id}/intercept-mode",
            json={"mode": "call_and_reply"},
        )
        self.assertEqual(set_mode.status_code, 200)
        logs = self.client.get(f"/supervisor/api/sessions/{self.safe_session_id}/logs?limit=15")
        self.assertEqual(logs.status_code, 200)
        payload = logs.json()
        self.assertEqual(payload["session_id"], self.safe_session_id)
        self.assertIn("logs", payload)
        self.assertGreaterEqual(len(payload["logs"]), 1)

    def test_supervisor_websocket_snapshot(self) -> None:
        self._seed_log()
        with self.client.websocket_connect("/supervisor/ws") as socket:
            first = socket.receive_json()
            self.assertEqual(first.get("type"), "snapshot")
            data = first.get("data", {})
            self.assertIn("sidecars", data)
            self.assertIn("sessions", data)
            self.assertIn("logs_by_session", data)
            self.assertIn("graphs_by_session", data)
            session_rows = data.get("sessions", [])
            self.assertTrue(session_rows)
            self.assertIn("can_send_message", session_rows[0])
            graphs = data.get("graphs_by_session", {})
            self.assertIsInstance(graphs, dict)
            if self.safe_session_id in graphs:
                table = graphs[self.safe_session_id]
                self.assertIsInstance(table, dict)
                self.assertIn("rows", table)
                self.assertIsInstance(table["rows"], list)

    def test_sidecar_state_transitions(self) -> None:
        instance_id = f"sc-{uuid.uuid4().hex[:8]}"
        attached_session = f"ut-sidecar-attached-{uuid.uuid4().hex[:8]}"
        attached_safe = self._add_empty_session_dir(attached_session)

        async def scenario() -> None:
            await SUPERVISOR.register_sidecar_connection(
                instance_id=instance_id,
                connection=FakeSidecarSocket(),
                workdir=str(PROJECT_ROOT),
                local="host 127.0.0.1",
                session_id="",
                pid=123,
                remote="http://127.0.0.1:8765",
                tool_url="",
            )
            rows = await SUPERVISOR.list_sidecars_summary()
            row = next((item for item in rows if item.get("instance_id") == instance_id), None)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.get("state"), "Idle")

            await SUPERVISOR.update_sidecar_connection(instance_id, session_id="missing-session")
            rows = await SUPERVISOR.list_sidecars_summary()
            row = next((item for item in rows if item.get("instance_id") == instance_id), None)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.get("state"), "Tentative")

            await SUPERVISOR.update_sidecar_connection(instance_id, session_id=attached_safe)
            rows = await SUPERVISOR.list_sidecars_summary()
            row = next((item for item in rows if item.get("instance_id") == instance_id), None)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.get("state"), "Attached")

            await SUPERVISOR.mark_sidecar_disconnected(instance_id)
            rows = await SUPERVISOR.list_sidecars_summary()
            row = next((item for item in rows if item.get("instance_id") == instance_id), None)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.get("state"), "Disconnected")

        asyncio.run(scenario())

    def test_sidecar_command_api_returns_result(self) -> None:
        instance_id = f"sc-api-{uuid.uuid4().hex[:8]}"

        async def responder(payload: dict) -> None:
            await SUPERVISOR.resolve_sidecar_command(
                instance_id,
                payload["command_id"],
                {
                    "type": "command_result",
                    "command_id": payload["command_id"],
                    "command": payload.get("command"),
                    "ok": True,
                    "result": {"ack": True},
                    "message": "ok",
                },
            )

        socket = FakeSidecarSocket(responder=responder)

        async def scenario() -> None:
            await SUPERVISOR.register_sidecar_connection(
                instance_id=instance_id,
                connection=socket,
                workdir=str(PROJECT_ROOT),
                local="host 127.0.0.1",
                session_id="",
                pid=123,
                remote="http://127.0.0.1:8765",
                tool_url="",
            )

        asyncio.run(scenario())
        response = self.client.post(
            f"/supervisor/api/sidecars/{instance_id}/command",
            json={"command": "set_session", "args": {"session": "abc"}},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertIn("result", payload)
        self.assertTrue(payload["result"].get("ok"))
        self.assertTrue(socket.sent)

    def test_sidecar_command_api_rejects_offline_sidecar(self) -> None:
        instance_id = f"sc-offline-{uuid.uuid4().hex[:8]}"

        async def scenario() -> None:
            await SUPERVISOR.register_sidecar_connection(
                instance_id=instance_id,
                connection=FakeSidecarSocket(),
                workdir=str(PROJECT_ROOT),
                local="host 127.0.0.1",
                session_id="",
                pid=123,
                remote="http://127.0.0.1:8765",
                tool_url="",
            )
            await SUPERVISOR.mark_sidecar_disconnected(instance_id)

        asyncio.run(scenario())
        response = self.client.post(
            f"/supervisor/api/sidecars/{instance_id}/command",
            json={"command": "set_session", "args": {"session": "abc"}},
        )
        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload.get("error"), "sidecar_offline")

    def test_session_graph_update_event_after_content_mutation(self) -> None:
        bundle_id = f"b_evt_{uuid.uuid4().hex[:8]}"
        with self.client.websocket_connect("/supervisor/ws") as socket:
            first = socket.receive_json()
            self.assertEqual(first.get("type"), "snapshot")
            try:
                asyncio.run(
                    call_tool(
                        "logic_set_bundle",
                        {
                            "id": bundle_id,
                            "bundle": "(declare-const evt_symbol Int)",
                            "motivation": {"rationale": "event coverage test"},
                        },
                    )
                )
                found = False
                for _ in range(8):
                    message = socket.receive_json()
                    if message.get("type") == "event" and message.get("event") == "session_graph_updated":
                        payload = message.get("data", {})
                        self.assertEqual(payload.get("session_id"), "default")
                        table = payload.get("table")
                        self.assertIsInstance(table, dict)
                        self.assertIn("rows", table)
                        self.assertTrue(
                            any(isinstance(row, dict) and row.get("id") == bundle_id for row in table["rows"])
                        )
                        found = True
                        break
                self.assertTrue(found)
            finally:
                asyncio.run(call_tool("logic_remove_bundle", {"id": bundle_id}))

    def test_default_intercept_timeout_is_ten_minutes(self) -> None:
        self.assertEqual(SUPERVISOR._timeout_seconds, 600.0)

    def test_message_api_returns_offline_when_no_active_session(self) -> None:
        self._seed_log()
        response = self.client.post(
            f"/supervisor/api/sessions/{self.safe_session_id}/messages",
            json={"message": "hello"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["delivered"])
        self.assertEqual(payload["reason"], "session_offline")

    def test_message_api_delivers_when_active_session_registered(self) -> None:
        async def scenario() -> None:
            await SUPERVISOR.register_active_session(self.safe_session_id, FakeSession())

        asyncio.run(scenario())
        response = self.client.post(
            f"/supervisor/api/sessions/{self.safe_session_id}/messages",
            json={
                "message": "Please run compact check",
                "level": "notice",
                "title": "Supervisor Request",
                "source": "supervisor-ui",
                "tags": ["ops", "priority"],
                "context": {"ticket": "ABC-1"},
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["delivered"])

    def test_session_summary_can_send_message_toggles(self) -> None:
        self._seed_log()
        before = self.client.get("/supervisor/api/sessions").json()
        row_before = next((row for row in before.get("sessions", []) if row["session_id"] == self.safe_session_id), None)
        self.assertIsNotNone(row_before)
        self.assertFalse(row_before["can_send_message"])
        self.assertEqual(row_before["connected_clients"], 0)
        self.assertTrue(bool(row_before.get("can_remove_session")))
        asyncio.run(SUPERVISOR.register_active_session(self.safe_session_id, FakeSession()))
        after = self.client.get("/supervisor/api/sessions").json()
        row_after = next((row for row in after.get("sessions", []) if row["session_id"] == self.safe_session_id), None)
        self.assertIsNotNone(row_after)
        self.assertTrue(row_after["can_send_message"])
        self.assertEqual(row_after["connected_clients"], 1)

    def test_reset_session_api_clears_inventory_and_logs(self) -> None:
        safe = self._add_empty_session_dir(f"ut-reset-{uuid.uuid4().hex[:8]}")
        engine = get_engine(safe)
        engine.set_rule(
            {
                "id": "r_reset",
                "lang": "pyexpr",
                "rule": "x > 0",
                "intent": "Reset smoke",
                "motivation": {"rationale": "seed reset test"},
            }
        )
        log_file = STORE_DIR / safe / "log.jsonl"
        append_tool_log(
            safe,
            {"name": "logic_list", "arguments": {}},
            {"ok": True, "result": {"items": []}},
            request_duration_ms=5,
        )
        self._extra_log_files.append(log_file)
        response = self.client.post(f"/supervisor/api/sessions/{safe}/reset", json={"wipe_logs": True})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("wiped_inventory"))
        self.assertTrue(payload.get("wiped_logs"))
        read_out = engine.list_items({})
        self.assertTrue(read_out.get("ok"))
        items = read_out.get("result", {}).get("items", [])
        self.assertEqual(items, [])

    def test_session_summary_counts_multiple_connected_clients(self) -> None:
        async def scenario() -> None:
            await SUPERVISOR.register_active_session(self.safe_session_id, FakeSession())
            await SUPERVISOR.register_active_session(self.safe_session_id, FakeSession())

        asyncio.run(scenario())
        payload = self.client.get("/supervisor/api/sessions").json()
        row = next((item for item in payload.get("sessions", []) if item["session_id"] == self.safe_session_id), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["connected_clients"], 2)


if __name__ == "__main__":
    unittest.main()
