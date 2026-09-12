"""Wake registration under delayed replies, disconnects, and early aborts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from codex_mcp_longrun.bridge_protocol import BridgeError, request_bridge
from codex_mcp_longrun.bridge_state import BridgeState
from codex_mcp_longrun.session_bridge import SessionBridge
from tests.test_bridge import FakeAppServer
from tests.test_session_bridge import Peer
from tests.test_server import server, TEST_ROOT


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="longrun-registration-")
        self.root = Path(self.tmp.name)
        self.bridge = SessionBridge(self.root / "app", self.root / "bridge", self.root / "state")
        self.thread, self.job = str(uuid.uuid4()), uuid.uuid4().hex
        self.peer = Peer(self.bridge, self.thread)
        self.bridge.app = self.peer
        self.request = {"version": 1, "action": "prepare", "job_id": self.job,
                        "thread_id": self.thread, "timeout_sec": 30, "grace_period_sec": 1,
                        "wake_policy": "auto", "call_id": "fixture-call"}
        self.context = SimpleNamespace(request_context=SimpleNamespace(
            meta={"threadId": self.thread, "callId": "fixture-call"}))
        self.socket_server = None
        self.tasks = []
        await self.bridge.observe(self.peer, "item/started", {
            "threadId": self.thread, "turnId": self.peer.turn["id"],
            "item": {"type": "mcpToolCall", "server": "longrun", "tool": "start_job",
                     "id": "fixture-call"}})

    async def asyncTearDown(self):
        if self.socket_server is not None:
            self.socket_server.close()
            await self.socket_server.wait_closed()
        for task in [*self.tasks, *self.bridge._connections,
                     *self.bridge._delivery_tasks.values(), *self.bridge._deadline_tasks.values()]:
            task.cancel()
        await asyncio.gather(*self.tasks, *self.bridge._connections,
                             *self.bridge._delivery_tasks.values(), *self.bridge._deadline_tasks.values(),
                             return_exceptions=True)
        self.bridge.state.close()
        self.tmp.cleanup()

    async def listen(self):
        try:
            self.socket_server = await asyncio.start_unix_server(
                self.bridge._handle_connection, path=str(self.bridge.bridge_socket))
        except PermissionError as exc:
            self.skipTest(f"sandbox does not permit Unix sockets: {exc}")
        self.bridge.bridge_socket.chmod(0o600)

    def block(self, method):
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = self.bridge.app.call

        async def call(name, params):
            if name == method:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return await original(name, params)

        self.bridge.app.call = call
        return entered, release, cancelled

    async def test_abort_before_first_snapshot_prevents_late_registration_and_survives_reopen(self):
        for active in (False, True):
            with self.subTest(active=active):
                if active:
                    self.bridge.app = FakeAppServer(self.thread)
                self.job = uuid.uuid4().hex
                request = {**self.request, "job_id": self.job}
                entered, release, _ = self.block("thread/goal/get")
                task = asyncio.create_task(self.bridge._dispatch(request))
                self.tasks.append(task)
                await asyncio.wait_for(entered.wait(), 1)
                self.assertIsNone(self.bridge.state.get(self.job))
                await self.bridge._dispatch({**request, "action": "abort"})
                release.set()
                with self.assertRaisesRegex(RuntimeError, "revoked"):
                    await task
                self.assertIsNone(self.bridge.state.get(self.job))
                self.assertNotIn("thread/goal/set", [name for name, _ in self.bridge.app.calls])
                reopened = BridgeState(self.root / "state")
                try:
                    with self.assertRaisesRegex(RuntimeError, "revoked"):
                        reopened.check_registration(self.job)
                finally:
                    reopened.close()

    async def test_delayed_prepare_longer_than_old_client_deadline_succeeds(self):
        await self.listen()
        original = self.peer.call

        async def call(method, params):
            if method == "thread/turns/list":
                await asyncio.sleep(5.2)
            return await original(method, params)

        self.peer.call = call
        started = time.monotonic()
        with patch.object(server, "BRIDGE_SOCKET", str(self.bridge.bridge_socket)):
            result, _ = await server._prepare_wake_registration(
                job_id=self.job, ctx=self.context, wake_policy="auto", timeout_sec=30, grace_period_sec=1)
        self.assertGreater(time.monotonic() - started, 5)
        self.assertEqual(result.wake_mode, "session")
        self.assertEqual(self.bridge.state.get(self.job).state, "armed")
        self.assertIsNone(self.peer.goal)

    async def test_socket_timeout_before_snapshot_cancels_prepare_without_late_lease(self):
        await self.listen()
        _, release, cancelled = self.block("thread/goal/get")
        with self.assertRaisesRegex(BridgeError, "prepare request timed out after"):
            await request_bridge(self.bridge.bridge_socket, self.request, timeout_sec=0.1)
        await asyncio.wait_for(cancelled.wait(), 1)
        # A lost abort reply must not be required to stop the original handler.
        release.set()
        self.assertIsNone(self.bridge.state.get(self.job))

    async def test_socket_disconnect_cancels_session_prepare_and_revokes_lease(self):
        await self.listen()
        entered, release, cancelled = self.block("thread/turns/list")
        _, writer = await asyncio.open_unix_connection(str(self.bridge.bridge_socket))
        try:
            writer.write(json.dumps(self.request).encode() + b"\n")
            await writer.drain()
            await asyncio.wait_for(entered.wait(), 1)
        finally:
            writer.close()
            await writer.wait_closed()
        await asyncio.wait_for(cancelled.wait(), 1)
        release.set()
        self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")
        self.assertNotIn(self.job, self.bridge.lease_peers)

    async def test_handler_cancellation_waits_for_prepare_cleanup(self):
        await self.listen()
        entered, _, cancelled = self.block("thread/turns/list")
        client = asyncio.create_task(request_bridge(self.bridge.bridge_socket, self.request))
        self.tasks.append(client)
        await asyncio.wait_for(entered.wait(), 1)
        handlers = list(self.bridge._connections)
        self.assertEqual(len(handlers), 1)
        handlers[0].cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")

    async def test_extra_request_bytes_are_rejected_and_revoke_preparation(self):
        await self.listen()
        entered, _, cancelled = self.block("thread/turns/list")
        reader, writer = await asyncio.open_unix_connection(str(self.bridge.bridge_socket))
        try:
            writer.write(json.dumps(self.request).encode() + b"\n")
            await writer.drain()
            await asyncio.wait_for(entered.wait(), 1)
            writer.write(b"x")
            await writer.drain()
            response = json.loads(await asyncio.wait_for(reader.readline(), 1))
            self.assertFalse(response["ok"])
            self.assertIn("unexpected data after bridge request", response["error"])
        finally:
            writer.close()
            await writer.wait_closed()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")

    async def test_cancelled_submission_releases_reserved_job_without_command_start(self):
        await self.listen()
        entered, _, _ = self.block("thread/turns/list")
        with patch.object(server, "BRIDGE_SOCKET", str(self.bridge.bridge_socket)), \
                patch.object(server, "_run_background_job", new_callable=AsyncMock) as run, \
                patch.object(server.uuid, "uuid4", return_value=SimpleNamespace(hex=self.job)):
            submission = asyncio.create_task(server.start_job(
                argv=["/usr/bin/true"], cwd=str(TEST_ROOT), ctx=self.context,
                wake_policy="auto", timeout_sec=5))
            self.tasks.append(submission)
            await asyncio.wait_for(entered.wait(), 1)
            submission.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await submission
            run.assert_not_called()
        self.assertEqual(server._read_job_metadata(self.job)["state"], "spawn_error")
        self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")

    async def test_session_deadline_reports_stage_and_never_starts_command(self):
        await self.listen()
        _, release, cancelled = self.block("thread/turns/list")
        with patch("codex_mcp_longrun.bridge.PREPARE_HANDLER_TIMEOUT_SEC", 0.05), \
                patch.object(server, "BRIDGE_SOCKET", str(self.bridge.bridge_socket)), \
                patch.object(server, "_run_background_job", new_callable=AsyncMock) as run, \
                patch.object(server.uuid, "uuid4", return_value=SimpleNamespace(hex=self.job)):
            with self.assertRaisesRegex(RuntimeError, "session/read-current-turn"):
                await server.start_job(argv=["/usr/bin/true"], cwd=str(TEST_ROOT),
                                       ctx=self.context, wake_policy="auto", timeout_sec=5)
            run.assert_not_called()
        self.assertTrue(cancelled.is_set())
        release.set()
        self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")
        metadata = server._read_job_metadata(self.job)
        self.assertEqual(metadata["state"], "spawn_error")
        self.assertIn("before command start", metadata["error"])
        self.assertIn("session/read-current-turn", metadata["error"])

    async def test_inner_rpc_failure_reports_stage(self):
        original = self.peer.call

        async def call(method, params):
            if method == "thread/turns/list":
                raise TimeoutError("fixture reply missing")
            return await original(method, params)

        self.peer.call = call
        with self.assertRaisesRegex(RuntimeError, "session/read-current-turn.*fixture reply missing"):
            await self.bridge._dispatch(self.request)
        self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")

    async def test_duplicate_session_prepare_cannot_succeed_while_first_is_pending(self):
        entered, release, _ = self.block("thread/turns/list")
        task = asyncio.create_task(self.bridge._dispatch(self.request))
        self.tasks.append(task)
        await asyncio.wait_for(entered.wait(), 1)
        with self.assertRaisesRegex(RuntimeError, "non-reusable"):
            await self.bridge._dispatch(self.request)
        release.set()
        self.assertTrue((await task)["automatic_wakeup"])

    async def test_repeated_prepare_cannot_accept_changed_inactive_goal(self):
        await self.bridge._dispatch(self.request)
        self.peer.goal = {"status": "blocked", "objective": "another task"}
        with self.assertRaisesRegex(RuntimeError, "non-reusable"):
            await self.bridge._dispatch(self.request)

    async def test_aborting_another_thread_cannot_revoke_lease(self):
        await self.bridge._dispatch(self.request)
        with self.assertRaisesRegex(RuntimeError, "another thread"):
            await self.bridge._dispatch({**self.request, "action": "abort", "thread_id": str(uuid.uuid4())})
        self.bridge.state.check_registration(self.job)
        self.assertEqual(self.bridge.state.get(self.job).state, "armed")


if __name__ == "__main__":
    unittest.main()
