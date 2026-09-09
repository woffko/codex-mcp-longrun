from __future__ import annotations

import asyncio
from dataclasses import replace
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

    async def test_manual_goal_pause_after_bridge_pause_abandons_lease(self) -> None:
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
        bridge_pause_revision = dict(self.fake.goal)
        await self.bridge._on_notification(
            "thread/goal/updated",
            {"threadId": self.thread_id, "goal": bridge_pause_revision},
        )

        # The user pauses the Goal again. It has the same status but a newer
        # revision, so the bridge must abandon automatic wakeup.
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


class GoalPauseOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await BridgeTests.asyncSetUp(self)
        self.job_id = uuid.uuid4().hex
        self.request = {"version": 1, "action": "prepare", "job_id": self.job_id,
                        "thread_id": self.thread_id, "timeout_sec": 600, "grace_period_sec": 5}

    async def asyncTearDown(self) -> None:
        await BridgeTests.asyncTearDown(self)

    def before_pause_reply(self, callback) -> None:
        original = self.fake.call
        async def call(method, params):
            result = await original(method, params)
            if method == "thread/goal/set" and params.get("status") == "paused":
                await callback(dict(result["goal"]))
            return result
        self.fake.call = call

    async def notify(self, goal) -> None:
        await self.bridge._on_notification("thread/goal/updated", {"threadId": self.thread_id, "goal": goal})

    def assert_not_rearmed(self) -> None:
        lease = self.bridge.state.get(self.job_id)
        self.assertEqual((lease.state, lease.delivery_state), ("abandoned", "abandoned"))
        self.assertNotIn(self.job_id, self.bridge._deadline_tasks)
        self.assertNotIn(self.thread_id, self.bridge._pause_notifications)

    async def complete(self) -> None:
        self.fake.thread_status = "idle"
        await self.bridge._terminal(self.job_id, {"thread_id": self.thread_id, "terminal_state": "succeeded"})
        task = self.bridge._delivery_tasks.get(self.job_id)
        if task:
            await task

    async def force_deadline(self) -> None:
        task = self.bridge._deadline_tasks.pop(self.job_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.bridge._expire_at_deadline(replace(self.bridge.state.get(self.job_id), deadline_at=0))
        task = self.bridge._delivery_tasks.get(self.job_id)
        if task:
            await task

    async def test_notification_before_reply_uses_normal_terminal_delivery(self) -> None:
        self.before_pause_reply(self.notify)
        await self.bridge._dispatch(self.request)
        lease = self.bridge.state.get(self.job_id)
        self.assertEqual((lease.state, lease.delivery_state), ("armed", "pending"))
        await self.complete()
        lease = self.bridge.state.get(self.job_id)
        self.assertEqual((lease.state, lease.delivery_state, lease.terminal_state), ("delivered", "resumed", "succeeded"))

    async def test_notification_after_reply_and_duplicate_do_not_revoke_pause(self) -> None:
        await self.bridge._dispatch(self.request)
        await self.notify(dict(self.fake.goal))
        await self.notify(dict(self.fake.goal))
        await self.complete()
        self.assertEqual(self.bridge.state.get(self.job_id).delivery_state, "resumed")

    async def test_pause_can_share_the_active_snapshots_second(self) -> None:
        original = self.fake.call
        timestamp = self.fake.goal["updatedAt"]
        async def call(method, params):
            result = await original(method, params)
            if method == "thread/goal/set" and params.get("status") == "paused":
                self.fake.goal["updatedAt"] = timestamp
                result = {"goal": dict(self.fake.goal)}
                await self.notify(result["goal"])
            return result
        self.fake.call = call
        await self.bridge._dispatch(self.request)
        await self.complete()
        self.assertEqual(self.bridge.state.get(self.job_id).delivery_state, "resumed")

    async def test_active_accounting_event_before_pause_can_share_its_timestamp(self) -> None:
        async def accounting(goal):
            await self.notify({**goal, "status": "active"})
            await self.notify(goal)
        self.before_pause_reply(accounting)
        await self.bridge._dispatch(self.request)
        await self.complete()
        self.assertEqual(self.bridge.state.get(self.job_id).delivery_state, "resumed")

    async def test_explicit_same_second_pause_during_registration_is_not_rearmed(self) -> None:
        async def manual_pause(goal):
            await self.notify(goal)
            await self.bridge.user_goal_control(self.thread_id, lambda: self.notify(goal))
        self.before_pause_reply(manual_pause)
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            await self.bridge._dispatch(self.request)
        self.assert_not_rearmed()

    async def test_explicit_same_second_pause_after_registration_revokes_wake(self) -> None:
        await self.bridge._dispatch(self.request)
        goal = dict(self.fake.goal)
        await self.bridge.user_goal_control(self.thread_id, lambda: self.notify(goal))
        await self.complete()
        self.assertEqual(self.fake.goal["status"], "paused")
        self.assertEqual(self.bridge.state.get(self.job_id).delivery_state, "abandoned")

    async def test_user_pause_is_forwarded_after_an_inflight_activation(self) -> None:
        await self.bridge._dispatch(self.request)
        entered, release = asyncio.Event(), asyncio.Event()
        user_forwarded = asyncio.Event()
        original = self.fake.call
        async def call(method, params):
            result = await original(method, params)
            if method == "thread/goal/set" and params.get("status") == "active":
                entered.set()
                await release.wait()
            return result
        self.fake.call = call
        self.fake.thread_status = "idle"
        await self.bridge._terminal(self.job_id, {"thread_id": self.thread_id, "terminal_state": "succeeded"})
        delivery = self.bridge._delivery_tasks[self.job_id]
        await entered.wait()
        async def user_pause():
            self.fake.goal["status"] = "paused"
            user_forwarded.set()
        user = asyncio.create_task(self.bridge.user_goal_control(self.thread_id, user_pause))
        await asyncio.sleep(0)
        self.assertFalse(user_forwarded.is_set())
        release.set()
        await delivery
        await user
        self.assertTrue(user_forwarded.is_set())
        self.assertEqual(self.fake.goal["status"], "paused")

    async def test_superseded_active_notification_does_not_revoke_owned_pause(self) -> None:
        old = dict(self.fake.goal)
        async def notifications(goal):
            await self.notify(old)
            await self.notify(goal)
        self.before_pause_reply(notifications)
        await self.bridge._dispatch(self.request)
        await self.notify(old)
        await self.complete()
        self.assertEqual(self.bridge.state.get(self.job_id).delivery_state, "resumed")

    async def test_manual_pause_during_rpc_is_never_rearmed(self) -> None:
        async def manual_pause(goal):
            await self.notify(goal)
            self.fake.goal["updatedAt"] += 1
            await self.notify(dict(self.fake.goal))
        self.before_pause_reply(manual_pause)
        with self.assertRaisesRegex(RuntimeError, "Goal changed"):
            await self.bridge._dispatch(self.request)
        self.assert_not_rearmed()
        self.fake.thread_status = "idle"
        await self.force_deadline()
        self.assertEqual(self.fake.goal["status"], "paused")

    async def test_unmatched_buffered_notification_revokes_even_if_latest_matches(self) -> None:
        async def extra_revision(goal):
            await self.notify({**goal, "updatedAt": goal["updatedAt"] + 1})
        self.before_pause_reply(extra_revision)
        with self.assertRaisesRegex(RuntimeError, "outside the bridge"):
            await self.bridge._dispatch(self.request)
        self.assert_not_rearmed()

    async def test_clear_during_pause_is_never_rearmed(self) -> None:
        async def clear(goal):
            await self.bridge._on_notification("thread/goal/cleared", {"threadId": self.thread_id})
        self.before_pause_reply(clear)
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            await self.bridge._dispatch(self.request)
        self.assert_not_rearmed()

    async def test_abort_during_pause_is_never_rearmed(self) -> None:
        async def abort(goal):
            await self.bridge._abort(self.job_id, {"thread_id": self.thread_id})
        self.before_pause_reply(abort)
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            await self.bridge._dispatch(self.request)
        self.assert_not_rearmed()

    async def test_early_terminal_waits_for_pause_confirmation(self) -> None:
        async def terminal(goal):
            result = await self.bridge._terminal(self.job_id, {"thread_id": self.thread_id, "terminal_state": "succeeded"})
            self.assertEqual(result["delivery_state"], "waiting_for_registration")
            self.assertNotIn(self.job_id, self.bridge._delivery_tasks)
            await self.notify(goal)
        self.before_pause_reply(terminal)
        self.fake.thread_status = "idle"
        await self.bridge._dispatch(self.request)
        await self.bridge._delivery_tasks[self.job_id]
        lease = self.bridge.state.get(self.job_id)
        self.assertEqual((lease.delivery_state, lease.terminal_state), ("resumed", "succeeded"))
        self.assertNotIn(self.job_id, self.bridge._deadline_tasks)

    async def test_pause_rpc_failure_leaves_no_armed_deadline(self) -> None:
        async def lost_reply(goal):
            await self.notify(goal)
            raise TimeoutError("synthetic lost pause reply")
        self.before_pause_reply(lost_reply)
        with self.assertRaises(TimeoutError):
            await self.bridge._dispatch(self.request)
        self.assert_not_rearmed()

    async def test_cancelled_pause_request_is_revoked(self) -> None:
        entered = asyncio.Event()
        async def pending_reply(goal):
            entered.set()
            await asyncio.Future()
        self.before_pause_reply(pending_reply)
        task = asyncio.create_task(self.bridge._dispatch(self.request))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_not_rearmed()

    async def test_duplicate_prepare_cannot_claim_success_before_confirmation(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        async def pending_reply(goal):
            entered.set()
            await release.wait()
        self.before_pause_reply(pending_reply)
        task = asyncio.create_task(self.bridge._dispatch(self.request))
        await entered.wait()
        try:
            with self.assertRaisesRegex(RuntimeError, "non-reusable"):
                await self.bridge._dispatch(self.request)
        finally:
            release.set()
            await task
        self.assertTrue((await self.bridge._dispatch(self.request))["idempotent"])

    async def test_new_paused_revision_without_notification_cannot_wake(self) -> None:
        await self.bridge._dispatch(self.request)
        self.fake.goal["updatedAt"] += 1
        await self.complete()
        self.assertEqual(self.bridge.state.get(self.job_id).delivery_state, "abandoned")
        self.assertEqual(self.fake.goal["status"], "paused")

    async def test_deadline_does_not_revive_old_inconsistent_lease(self) -> None:
        await self.bridge._dispatch(self.request)
        lease = self.bridge.state.update(self.job_id, delivery_state="abandoned")
        self.assertIn(lease, self.bridge.state.pending())
        self.fake.thread_status = "idle"
        await self.force_deadline()
        self.assertEqual(self.fake.goal["status"], "paused")
        self.assertEqual(self.bridge.state.get(self.job_id).delivery_state, "abandoned")
        self.bridge._recover_lease(lease)
        self.assertEqual(self.bridge.state.get(self.job_id).state, "abandoned")

    async def test_fallback_checks_paused_revision_when_notification_is_missing(self) -> None:
        await self.bridge._dispatch(self.request)
        self.fake.goal["updatedAt"] += 1
        self.fake.thread_status = "idle"
        await self.force_deadline()
        self.assertEqual(self.bridge.state.get(self.job_id).delivery_state, "abandoned")
        self.assertEqual(self.fake.goal["status"], "paused")

    async def test_duplicate_terminal_during_activation_cannot_reset_delivery(self) -> None:
        await self.bridge._dispatch(self.request)
        original = self.fake.call
        async def call(method, params):
            result = await original(method, params)
            if method == "thread/goal/set" and params.get("status") == "active":
                await self.bridge._terminal(self.job_id, {"thread_id": self.thread_id, "terminal_state": "failed"})
                self.assertEqual(self.bridge.state.get(self.job_id).delivery_state, "activating")
            return result
        self.fake.call = call
        await self.complete()
        self.assertEqual(self.bridge.state.get(self.job_id).terminal_state, "succeeded")

    async def test_lost_activation_reply_and_restart_never_retry(self) -> None:
        await self.bridge._dispatch(self.request)
        original = self.fake.call
        async def call(method, params):
            result = await original(method, params)
            if method == "thread/goal/set" and params.get("status") == "active":
                raise TimeoutError("synthetic lost activation reply")
            return result
        self.fake.call = call
        await self.complete()
        self.assertEqual(self.bridge.state.get(self.job_id).delivery_state, "needs_manual_recovery")
        await self.complete()
        activating = self.bridge.state.update(self.job_id, state="terminal", delivery_state="activating")
        self.bridge._recover_lease(activating)
        self.assertEqual(self.bridge.state.get(self.job_id).delivery_state, "needs_manual_recovery")
        self.assertEqual([p["status"] for m, p in self.fake.calls if m == "thread/goal/set"], ["paused", "active"])

    async def test_cancelled_activation_requires_recovery_without_retry(self) -> None:
        await self.bridge._dispatch(self.request)
        original = self.fake.call
        entered = asyncio.Event()
        async def call(method, params):
            result = await original(method, params)
            if method == "thread/goal/set" and params.get("status") == "active":
                entered.set()
                await asyncio.Future()
            return result
        self.fake.call = call
        self.fake.thread_status = "idle"
        await self.bridge._terminal(self.job_id, {"thread_id": self.thread_id, "terminal_state": "succeeded"})
        task = self.bridge._delivery_tasks[self.job_id]
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        lease = self.bridge.state.get(self.job_id)
        self.assertEqual(lease.delivery_state, "activating")
        self.bridge._recover_lease(lease)
        await self.complete()
        self.assertEqual(self.bridge.state.get(self.job_id).delivery_state, "needs_manual_recovery")
        self.assertEqual([p["status"] for m, p in self.fake.calls if m == "thread/goal/set"], ["paused", "active"])

    async def test_cancelled_user_control_keeps_its_revocation(self) -> None:
        await self.bridge._dispatch(self.request)
        lock = self.bridge._goal_controls.setdefault(self.thread_id, asyncio.Lock())
        forwarded = asyncio.Event()
        async def control():
            forwarded.set()
        await lock.acquire()
        task = asyncio.create_task(self.bridge.user_goal_control(self.thread_id, control))
        try:
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            lock.release()
        self.assertFalse(forwarded.is_set())
        self.assert_not_rearmed()


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
                        await connection.send(json.dumps({"method": "thread/goal/updated", "params": {
                            "threadId": thread_id, "goal": dict(goal)}}))
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
