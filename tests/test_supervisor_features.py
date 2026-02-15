from __future__ import annotations

import asyncio
import json
import unittest
import uuid

from starlette.testclient import TestClient

from logic_mcp.audit_log import append_tool_log
from logic_mcp.engine import call_tool
from logic_mcp.engine import server
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


class SupervisorFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session_id = f"ut-sup-{uuid.uuid4().hex[:8]}"
        self.safe_session_id = sanitize_namespace(self.session_id)
        self.session_dir = STORE_DIR / self.safe_session_id
        self.log_file = STORE_DIR / f"{self.safe_session_id}_log.jsonl"
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
        asyncio.run(self._reset_supervisor_state())

    async def _reset_supervisor_state(self) -> None:
        async with SUPERVISOR._lock:
            for item in SUPERVISOR._pending.values():
                if item.decision_event is not None and not item.decision_event.is_set():
                    item.decision_event.set()
            SUPERVISOR._pending.clear()
            SUPERVISOR._modes.clear()
            SUPERVISOR._active_targets.clear()
            SUPERVISOR._subscribers.clear()
            SUPERVISOR._timeout_seconds = 600.0

    def _seed_log(self) -> None:
        entry = {
            "time": "2026-02-13T10:00:00.000Z",
            "call": {"name": "logic_list", "arguments": {}},
            "response": {"ok": True, "result": {"items": []}},
            "request_duration_ms": 6,
        }
        self.log_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

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

    def test_session_graph_update_event_after_content_mutation(self) -> None:
        bundle_id = f"b_evt_{uuid.uuid4().hex[:8]}"
        with self.client.websocket_connect("/supervisor/ws") as socket:
            first = socket.receive_json()
            self.assertEqual(first.get("type"), "snapshot")
            try:
                asyncio.run(
                    call_tool(
                        "logic_set_bundle",
                        {"id": bundle_id, "bundle": "(declare-const evt_symbol Int)"},
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
        asyncio.run(SUPERVISOR.register_active_session(self.safe_session_id, FakeSession()))
        after = self.client.get("/supervisor/api/sessions").json()
        row_after = next((row for row in after.get("sessions", []) if row["session_id"] == self.safe_session_id), None)
        self.assertIsNotNone(row_after)
        self.assertTrue(row_after["can_send_message"])
        self.assertEqual(row_after["connected_clients"], 1)

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
