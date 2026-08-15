from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, unix_serve

from codex_mcp_longrun.bridge import GoalBridge
from codex_mcp_longrun.bridge_protocol import request_bridge


class FakeAppServer:
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.goal: dict[str, Any] = {
            "threadId": thread_id,
            "objective": "Complete the bridge test.",
            "status": "active",
            "createdAt": 100,
            "updatedAt": 100,
        }
        self.thread_status = "active"
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "thread/goal/get":
            return {"goal": dict(self.goal)}
        if method == "thread/goal/set":
            self.goal["status"] = params["status"]
            self.goal["updatedAt"] += 1
            return {"goal": dict(self.goal)}
        if method == "thread/read":
            return {"thread": {"status": {"type": self.thread_status}}}
        raise AssertionError(f"unexpected App Server method: {method}")

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="codex-longrun-bridge-test-")
        root = Path(self.tempdir.name)
        self.thread_id = str(uuid.uuid4())
        self.bridge = GoalBridge(root / "app.sock", root / "bridge.sock", root / "state.sqlite3")
        self.fake = FakeAppServer(self.thread_id)
        self.bridge.app = self.fake  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        tasks = [*self.bridge._delivery_tasks.values(), *self.bridge._deadline_tasks.values()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.bridge.state.close()
        self.tempdir.cleanup()

    async def test_prepare_terminal_idle_reactivates_exact_goal_once(self) -> None:
        job_id = uuid.uuid4().hex
        prepared = await self.bridge._dispatch(
            {
                "version": 1,
                "action": "prepare",
                "job_id": job_id,
                "thread_id": self.thread_id,
                "timeout_sec": 60,
                "grace_period_sec": 5,
            }
        )
        self.assertTrue(prepared["automatic_wakeup"])
        self.assertEqual(self.fake.goal["status"], "paused")

        terminal = await self.bridge._dispatch(
            {
                "version": 1,
                "action": "terminal",
                "job_id": job_id,
                "thread_id": self.thread_id,
                "terminal_state": "succeeded",
            }
        )
        self.assertEqual(terminal["delivery_state"], "waiting_for_idle")
        await asyncio.sleep(0)
        self.assertEqual(self.fake.goal["status"], "paused")

        self.fake.thread_status = "idle"
        await self.bridge._on_notification(
            "thread/status/changed",
            {"threadId": self.thread_id, "status": {"type": "idle"}},
        )
        task = self.bridge._delivery_tasks[job_id]
        await asyncio.wait_for(task, timeout=2)

        self.assertEqual(self.fake.goal["status"], "active")
        status_sets = [
            params["status"]
            for method, params in self.fake.calls
            if method == "thread/goal/set"
        ]
        self.assertEqual(status_sets, ["paused", "active"])
        self.assertNotIn("turn/start", [method for method, _ in self.fake.calls])
        lease = self.bridge.state.get(job_id)
        assert lease is not None
        self.assertEqual(lease.delivery_state, "resumed")
        duplicate = await self.bridge._dispatch(
            {
                "version": 1,
                "action": "terminal",
                "job_id": job_id,
                "thread_id": self.thread_id,
                "terminal_state": "succeeded",
            }
        )
        self.assertTrue(duplicate["idempotent"])
        status_sets = [
            params["status"]
            for method, params in self.fake.calls
            if method == "thread/goal/set"
        ]
        self.assertEqual(status_sets, ["paused", "active"])

    async def test_manual_goal_resume_abandons_bridge_lease(self) -> None:
        job_id = uuid.uuid4().hex
        await self.bridge._dispatch(
            {
                "version": 1,
                "action": "prepare",
                "job_id": job_id,
                "thread_id": self.thread_id,
                "timeout_sec": 60,
                "grace_period_sec": 5,
            }
        )
        # Consume the expected notification generated by the bridge pause.
        await self.bridge._on_notification(
            "thread/goal/updated", {"threadId": self.thread_id, "goal": dict(self.fake.goal)}
        )
        self.fake.goal["status"] = "active"
        self.fake.goal["updatedAt"] += 1
        await self.bridge._on_notification(
            "thread/goal/updated", {"threadId": self.thread_id, "goal": dict(self.fake.goal)}
        )
        lease = self.bridge.state.get(job_id)
        assert lease is not None
        self.assertEqual(lease.delivery_state, "abandoned")

        result = await self.bridge._dispatch(
            {
                "version": 1,
                "action": "terminal",
                "job_id": job_id,
                "thread_id": self.thread_id,
                "terminal_state": "succeeded",
            }
        )
        self.assertTrue(result["idempotent"])
        status_sets = [
            params["status"]
            for method, params in self.fake.calls
            if method == "thread/goal/set"
        ]
        self.assertEqual(status_sets, ["paused"])


class BridgeSocketIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_socket_and_app_server_jsonrpc_round_trip(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-longrun-bridge-socket-test-") as temporary:
            root = Path(temporary)
            app_socket = root / "app.sock"
            bridge_socket = root / "bridge.sock"
            thread_id = str(uuid.uuid4())
            goal: dict[str, Any] = {
                "threadId": thread_id,
                "objective": "Exercise the private bridge protocol.",
                "status": "active",
                "createdAt": 10,
                "updatedAt": 10,
            }

            async def app_handler(connection: ServerConnection) -> None:
                async for raw in connection:
                    request = json.loads(raw)
                    request_id = request.get("id")
                    if request_id is None:
                        continue
                    method = request["method"]
                    if method == "initialize":
                        result: dict[str, Any] = {"userAgent": "fake-app-server"}
                    elif method == "thread/goal/get":
                        result = {"goal": dict(goal)}
                    elif method == "thread/goal/set":
                        goal["status"] = request["params"]["status"]
                        goal["updatedAt"] += 1
                        result = {"goal": dict(goal)}
                    elif method == "thread/read":
                        result = {"thread": {"status": {"type": "idle"}}}
                    else:
                        raise AssertionError(f"unexpected method: {method}")
                    await connection.send(json.dumps({"id": request_id, "result": result}))

            try:
                app_server = await unix_serve(app_handler, str(app_socket), compression=None)
            except PermissionError as exc:
                self.skipTest(f"sandbox does not permit Unix sockets: {exc}")
            bridge = GoalBridge(app_socket, bridge_socket, root / "state.sqlite3")
            await bridge.start()
            try:
                job_id = uuid.uuid4().hex
                prepared = await request_bridge(
                    bridge_socket,
                    {
                        "action": "prepare",
                        "job_id": job_id,
                        "thread_id": thread_id,
                        "timeout_sec": 60,
                        "grace_period_sec": 5,
                    },
                )
                self.assertTrue(prepared["automatic_wakeup"])
                self.assertEqual(goal["status"], "paused")
                await request_bridge(
                    bridge_socket,
                    {
                        "action": "terminal",
                        "job_id": job_id,
                        "thread_id": thread_id,
                        "terminal_state": "succeeded",
                    },
                )
                for _ in range(100):
                    lease = bridge.state.get(job_id)
                    if lease is not None and lease.delivery_state == "resumed":
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("bridge did not resume the fake Goal")
                self.assertEqual(goal["status"], "active")
                self.assertEqual(bridge_socket.stat().st_mode & 0o777, 0o600)
            finally:
                await bridge.close()
                app_server.close()
                await app_server.wait_closed()


if __name__ == "__main__":
    unittest.main()
