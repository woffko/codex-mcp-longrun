from __future__ import annotations

import asyncio
from contextlib import closing
import json
from pathlib import Path
import tempfile
import sqlite3
import unittest
import uuid

from codex_mcp_longrun.session_bridge import SessionBridge
from codex_mcp_longrun.bridge_state import BridgeState
from tests import test_bridge as goal_tests


class Peer:
    def __init__(self, bridge, thread_id):
        self.bridge, self.thread_id = bridge, thread_id
        self.goal = None
        self.turn = {"id": str(uuid.uuid4()), "status": "inProgress"}
        self.lock = asyncio.Lock()
        self.calls = []
        self.fail = None
        self.receipts = []
        self.wakes = 0
        self.terminal_during_receipt = None

    async def exclusive(self, operation):
        async with self.lock:
            return await operation()

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/goal/get":
            return {"goal": self.goal}
        if method in {"thread/turns/list", "thread/items/list", "thread/read"}:
            raise AssertionError("session continuation must not inspect persisted history")
        if self.fail == method:
            raise TimeoutError("ambiguous test RPC")
        if method == "turn/interrupt":
            assert params["turnId"] == self.turn["id"]
            self.turn["status"] = "interrupted"
            await self.bridge.observe(self, "turn/completed", {"threadId": self.thread_id, "turn": dict(self.turn)})
            return {}
        if method == "thread/inject_items":
            if self.terminal_during_receipt is not None:
                await self.bridge._dispatch(self.terminal_during_receipt)
                assert self.wakes == 0
            self.receipts.append(params["items"])
            return {}
        if method == "turn/start":
            self.wakes += 1
            self.turn = {"id": str(uuid.uuid4()), "status": "inProgress"}
            await self.bridge.observe(self, "turn/started", {"threadId": self.thread_id, "turn": dict(self.turn)})
            if self.fail == "turn/start-after":
                raise TimeoutError("server started the turn but its response was lost")
            return {"turn": dict(self.turn)}
        raise AssertionError(method)


class SessionBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bridge = SessionBridge(root / "app", root / "bridge", root / "state.db")
        self.thread = str(uuid.uuid4())
        self.job = uuid.uuid4().hex
        self.peer = Peer(self.bridge, self.thread)
        self.bridge.app = self.peer
        self.request = {"version": 1, "action": "prepare", "job_id": self.job,
                        "thread_id": self.thread, "timeout_sec": 30, "grace_period_sec": 1,
                        "wake_policy": "session", "call_id": "mcp-call"}

    async def asyncTearDown(self):
        for task in [*self.bridge._delivery_tasks.values(), *self.bridge._deadline_tasks.values()]:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.bridge.state.close()
        self.tmp.cleanup()

    async def observed(self, *, emit_started=True):
        if emit_started:
            await self.bridge.observe(self.peer, "turn/started", {
                "threadId": self.thread, "turn": dict(self.peer.turn)})
        await self.bridge.observe(self.peer, "item/started", {
            "threadId": self.thread, "turnId": self.peer.turn["id"],
            "item": {"type": "mcpToolCall", "server": "longrun", "tool": "start_job", "id": "mcp-call"}})

    async def prepare(self):
        await self.observed()
        return await self.bridge._dispatch(self.request)

    async def handoff(self):
        return await self.bridge._dispatch({**self.request, "action": "handoff", "job_state": "running"})

    async def terminal(self):
        return await self.bridge._dispatch({**self.request, "action": "terminal", "terminal_state": "succeeded"})

    async def delivery(self):
        task = self.bridge._delivery_tasks.get(self.job)
        if task:
            await task

    async def test_handoff_and_terminal_wake_once_without_goal(self):
        registered = await self.prepare()
        self.assertEqual(registered["wake_mode"], "session")
        self.assertTrue(registered["automatic_wakeup"])
        await self.handoff()
        self.assertEqual(self.peer.turn["status"], "interrupted")
        self.assertEqual(self.peer.wakes, 0)
        self.assertIn(self.job, json.dumps(self.peer.receipts))
        await self.terminal()
        await self.delivery()
        await self.terminal()
        await self.delivery()
        self.assertEqual(self.peer.wakes, 1)
        self.assertIsNone(self.peer.goal)
        lease = self.bridge.state.get(self.job)
        self.assertEqual(lease.delivery_state, "resumed")
        self.assertEqual(lease.wake_turn_id, self.peer.turn["id"])

    async def test_early_terminal_waits_for_confirmed_handoff(self):
        await self.prepare()
        await self.terminal()
        self.assertNotIn(self.job, self.bridge._delivery_tasks)
        self.assertEqual(self.peer.wakes, 0)
        await self.handoff()
        await self.delivery()
        self.assertEqual(self.peer.wakes, 1)

    async def test_new_user_input_cancels_only_wakeup(self):
        await self.prepare()
        await self.handoff()
        self.bridge.user_activity(self.thread)
        await self.terminal()
        self.assertEqual(self.peer.wakes, 0)
        self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")
        self.assertNotIn("cancel_job", [m for m, _ in self.peer.calls])

    async def test_manual_interrupt_before_handoff_prevents_wake(self):
        await self.prepare()
        await self.peer.call("turn/interrupt", {"turnId": self.peer.turn["id"]})
        with self.assertRaisesRegex(RuntimeError, "no longer live"):
            await self.handoff()
        await self.terminal()
        self.assertEqual(self.peer.wakes, 0)

    async def test_goal_limits_and_unknown_states_are_not_bypassed(self):
        for status in ("budgetLimited", "usageLimited", "unknown"):
            for policy in ("session", "auto", "goal"):
                self.peer.goal = {"status": status}
                with self.subTest(status=status, policy=policy), self.assertRaisesRegex(RuntimeError, "limits"):
                    await self.bridge._dispatch({**self.request, "wake_policy": policy})
        self.assertIsNone(self.bridge.state.get(self.job))

    async def test_all_hints_use_session_for_absent_or_inactive_goal(self):
        for status in (None, "complete", "paused", "blocked"):
            for policy in ("auto", "goal", "session"):
                goal = None if status is None else {"status": status, "objective": "old task", "updatedAt": 12}
                self.peer.goal = goal
                original = json.dumps(goal, sort_keys=True)
                self.request["job_id"] = self.job = uuid.uuid4().hex
                self.request["wake_policy"] = policy
                self.peer.turn = {"id": str(uuid.uuid4()), "status": "inProgress"}
                with self.subTest(status=status, policy=policy):
                    result = await self.prepare()
                    self.assertEqual(result["wake_mode"], "session")
                    self.assertTrue(result["state_based_routing"])
                    await self.handoff()
                    await self.terminal()
                    await self.delivery()
                    self.assertEqual(self.bridge.state.get(self.job).delivery_state, "resumed")
                    self.assertEqual(json.dumps(self.peer.goal, sort_keys=True), original)
                    self.assertNotIn("thread/goal/set", [method for method, _ in self.peer.calls])

    async def test_goal_created_while_waiting_prevents_wake(self):
        await self.prepare()
        await self.handoff()
        self.peer.goal = {"status": "active", "objective": "new task"}
        await self.terminal()
        await self.delivery()
        self.assertEqual(self.peer.wakes, 0)
        self.assertEqual(self.bridge.state.get(self.job).delivery_state, "needs_manual_recovery")

    async def test_lost_wake_reply_is_never_retried(self):
        await self.prepare()
        await self.handoff()
        self.peer.fail = "turn/start"
        await self.terminal()
        await self.delivery()
        await self.terminal()
        await self.delivery()
        self.assertEqual([m for m, _ in self.peer.calls].count("turn/start"), 1)
        self.assertEqual(self.bridge.state.get(self.job).delivery_state, "needs_manual_recovery")

    async def test_successful_wake_with_lost_reply_is_never_duplicated(self):
        await self.prepare()
        await self.handoff()
        self.peer.fail = "turn/start-after"
        await self.terminal()
        await self.delivery()
        await self.terminal()
        await self.delivery()
        self.assertEqual(self.peer.wakes, 1)
        self.assertEqual(self.bridge.state.get(self.job).delivery_state, "needs_manual_recovery")

    async def test_terminal_during_receipt_is_delivered_only_after_receipt(self):
        await self.prepare()
        self.peer.terminal_during_receipt = {**self.request, "action": "terminal", "terminal_state": "failed"}
        await self.handoff()
        await self.delivery()
        self.assertEqual(self.peer.wakes, 1)
        self.assertTrue(self.peer.receipts)

    async def test_second_peer_cannot_take_over_an_armed_lease(self):
        await self.prepare()
        other = Peer(self.bridge, self.thread)
        await self.bridge.observe(other, "turn/started", {"threadId": self.thread, "turn": dict(self.peer.turn)})
        with self.assertRaisesRegex(RuntimeError, "no longer live"):
            await self.handoff()
        self.assertIs(self.bridge.peers[self.thread], self.peer)
        self.assertEqual(other.calls, [])
        self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")

    async def test_lost_interrupt_or_receipt_reply_prevents_wake(self):
        for method in ("turn/interrupt", "thread/inject_items"):
            self.request["job_id"] = self.job = uuid.uuid4().hex
            self.peer.turn = {"id": str(uuid.uuid4()), "status": "inProgress"}
            self.peer.fail = method
            await self.prepare()
            with self.assertRaises(TimeoutError):
                await self.handoff()
            await self.terminal()
            await self.delivery()
            self.assertEqual(self.bridge.state.get(self.job).delivery_state, "needs_manual_recovery")
        self.assertEqual(self.peer.wakes, 0)

    async def test_disconnect_and_restart_never_replay_a_wake(self):
        await self.prepare()
        await self.handoff()
        lease = self.bridge.state.get(self.job)
        self.bridge._recover_lease(lease)
        self.assertEqual(self.bridge.state.get(self.job).delivery_state, "needs_manual_recovery")
        self.bridge.disconnected(self.peer)
        await self.terminal()
        self.assertEqual(self.peer.wakes, 0)

    async def test_job_cannot_be_rearmed_after_delivery(self):
        await self.prepare()
        await self.handoff()
        await self.terminal()
        await self.delivery()
        await self.observed()
        with self.assertRaisesRegex(RuntimeError, "non-reusable"):
            await self.bridge._dispatch(self.request)

    async def test_cancel_wakeup_has_no_goal_or_job_side_effect(self):
        await self.prepare()
        result = await self.bridge._dispatch({**self.request, "action": "cancel_wakeup"})
        self.assertFalse(result["job_cancelled"])
        self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")

    async def test_forged_call_cannot_register(self):
        with self.assertRaisesRegex(RuntimeError, "trusted MCP call"):
            await self.bridge._dispatch({**self.request, "call_id": None})
        self.assertIsNone(self.bridge.state.get(self.job))

    async def test_abort_during_slow_session_prepare_cannot_arm_late(self):
        await self.observed()
        entered = asyncio.Event()
        release = asyncio.Event()
        original_call = self.peer.call
        goal_reads = 0

        async def delayed_call(method, params):
            nonlocal goal_reads
            if method == "thread/goal/get":
                goal_reads += 1
            if method == "thread/goal/get" and goal_reads == 2:
                entered.set()
                await release.wait()
            return await original_call(method, params)

        self.peer.call = delayed_call
        task = asyncio.create_task(self.bridge._dispatch(self.request))
        await entered.wait()
        lease = self.bridge.state.get(self.job)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.state, "preparing")
        aborted = await self.bridge._dispatch({**self.request, "action": "abort"})
        self.assertEqual(aborted["delivery_state"], "abandoned")
        release.set()
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            await task
        self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")
        self.assertNotIn(self.job, self.bridge.lease_peers)

    async def test_abort_after_session_prepare_abandons_instead_of_goal_delivery(self):
        original = json.dumps(self.peer.goal, sort_keys=True)
        await self.prepare()
        aborted = await self.bridge._dispatch({**self.request, "action": "abort"})
        self.assertEqual(aborted["delivery_state"], "abandoned")
        self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")
        self.assertNotIn(self.job, self.bridge._delivery_tasks)
        self.assertEqual(json.dumps(self.peer.goal, sort_keys=True), original)

    async def test_resumed_live_mcp_item_seeds_turn_without_history_or_started_event(self):
        await self.observed(emit_started=False)
        await self.bridge._dispatch(self.request)
        await self.handoff()
        await self.terminal()
        await self.delivery()
        self.assertEqual(self.peer.wakes, 1)
        self.assertFalse(any(m in {"thread/turns/list", "thread/items/list", "thread/read"}
                             for m, _ in self.peer.calls))

    async def test_health_advertises_history_free_session_tracking_without_rpc(self):
        result = await self.bridge._dispatch({"version": 1, "action": "health"})
        self.assertTrue(result["session_history_free"])
        self.assertEqual(self.peer.calls, [])

    async def test_duplicate_completion_during_receipt_does_not_abandon_handoff(self):
        await self.prepare()
        original = self.peer.call
        async def call(method, params):
            if method == "thread/inject_items":
                await self.bridge.observe(self.peer, "turn/completed", {
                    "threadId": self.thread, "turn": dict(self.peer.turn)})
            return await original(method, params)
        self.peer.call = call
        await self.handoff()
        await self.terminal()
        await self.delivery()
        self.assertEqual(self.peer.wakes, 1)

    async def test_terminal_status_cannot_be_rewritten_by_a_conflicting_event(self):
        await self.observed()
        for status in ("completed", "interrupted"):
            await self.bridge.observe(self.peer, "turn/completed", {
                "threadId": self.thread, "turn": {**self.peer.turn, "status": status}})
        self.assertEqual((await self.bridge._latest_turn(self.peer, self.thread))["status"], "completed")

    async def test_summary_or_unknown_stream_is_not_current_turn_authority(self):
        with self.assertRaisesRegex(RuntimeError, "owning live event"):
            await self.bridge._latest_turn(self.peer, self.thread)
        self.assertEqual(self.peer.calls, [])

    async def test_late_item_and_duplicate_started_do_not_revive_completed_turn(self):
        await self.observed()
        self.peer.turn["status"] = "completed"
        await self.bridge.observe(self.peer, "turn/completed", {
            "threadId": self.thread, "turn": dict(self.peer.turn)})
        await self.bridge.observe(self.peer, "turn/started", {
            "threadId": self.thread, "turn": {**self.peer.turn, "status": "inProgress"}})
        await self.observed(emit_started=False)
        self.assertNotIn((self.thread, "mcp-call"), self.bridge.calls)
        self.assertEqual((await self.bridge._latest_turn(self.peer, self.thread))["status"], "completed")

    async def test_stale_item_cannot_replace_a_newer_live_turn(self):
        await self.observed()
        old = self.peer.turn["id"]
        self.peer.turn = {"id": str(uuid.uuid4()), "status": "inProgress"}
        await self.observed()
        await self.bridge.observe(self.peer, "item/started", {
            "threadId": self.thread, "turnId": old,
            "item": {"type": "mcpToolCall", "server": "longrun", "tool": "start_job", "id": "mcp-call"}})
        self.assertEqual(self.bridge.calls[(self.thread, "mcp-call")][0], self.peer.turn["id"])

    async def test_foreign_completion_cannot_confirm_owning_handoff(self):
        await self.prepare()
        other = Peer(self.bridge, self.thread)
        await self.bridge.observe(other, "turn/completed", {
            "threadId": self.thread, "turn": {**self.peer.turn, "status": "interrupted"}})
        self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")
        with self.assertRaisesRegex(RuntimeError, "no longer live"):
            await self.handoff()
        self.assertEqual(other.calls, [])

    async def test_wrong_turn_completion_does_not_release_interruption_wait(self):
        await self.prepare()
        original = self.peer.call
        async def call(method, params):
            if method == "turn/interrupt":
                await self.bridge.observe(self.peer, "turn/completed", {
                    "threadId": self.thread, "turn": {"id": str(uuid.uuid4()), "status": "interrupted"}})
                self.assertFalse(self.bridge.turn_done[(self.thread, self.peer.turn["id"])].is_set())
                self.assertEqual((await self.bridge._latest_turn(self.peer, self.thread))["status"], "inProgress")
            return await original(method, params)
        self.peer.call = call
        await self.handoff()
        await self.terminal()
        await self.delivery()
        self.assertEqual(self.peer.wakes, 1)

    async def test_new_turn_or_activity_during_goal_confirmation_cannot_arm_late(self):
        for change in ("new-turn", "user-activity", "disconnect"):
            with self.subTest(change=change):
                self.request["job_id"] = self.job = uuid.uuid4().hex
                self.peer = Peer(self.bridge, self.thread)
                self.bridge.disconnected(self.bridge.peers.get(self.thread))
                self.bridge.app = self.peer
                await self.observed()
                original = self.peer.call
                entered, release = asyncio.Event(), asyncio.Event()
                goal_reads = 0
                async def call(method, params):
                    nonlocal goal_reads
                    if method == "thread/goal/get":
                        goal_reads += 1
                        if goal_reads == 2:
                            entered.set()
                            await release.wait()
                    return await original(method, params)
                self.peer.call = call
                task = asyncio.create_task(self.bridge._dispatch(self.request))
                await asyncio.wait_for(entered.wait(), 1)
                if change == "new-turn":
                    self.peer.turn = {"id": str(uuid.uuid4()), "status": "inProgress"}
                    await self.bridge.observe(self.peer, "turn/started", {
                        "threadId": self.thread, "turn": dict(self.peer.turn)})
                elif change == "user-activity":
                    self.bridge.user_activity(self.thread)
                else:
                    self.bridge.disconnected(self.peer)
                release.set()
                with self.assertRaises(RuntimeError):
                    await task
                self.assertEqual(self.bridge.state.get(self.job).state, "abandoned")
                self.assertNotIn(self.job, self.bridge.lease_peers)


class GoalCompatibilityTests(goal_tests.BridgeTests):
    """Run the unchanged Goal lifecycle assertions against the new coordinator."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bridge.state.close()
        root = Path(self.tempdir.name)
        self.bridge = SessionBridge(root / "app.sock", root / "bridge.sock", root / "state.sqlite3")
        self.bridge.app = self.fake

    async def test_auto_with_active_goal_uses_original_goal_lifecycle(self):
        result = await self.bridge._dispatch({"version": 1, "action": "prepare", "job_id": uuid.uuid4().hex,
                                             "thread_id": self.thread_id, "timeout_sec": 30,
                                             "grace_period_sec": 1, "wake_policy": "auto"})
        self.assertTrue(result["automatic_wakeup"])
        self.assertEqual(self.fake.goal["status"], "paused")
        self.assertNotIn("turn/interrupt", [m for m, _ in self.fake.calls])

    async def test_session_hint_with_active_goal_uses_goal_mode(self):
        result = await self.bridge._dispatch({"version": 1, "action": "prepare", "job_id": uuid.uuid4().hex,
                                             "thread_id": self.thread_id, "timeout_sec": 30,
                                             "grace_period_sec": 1, "wake_policy": "session"})
        self.assertEqual(result["wake_mode"], "goal")
        self.assertTrue(result["state_based_routing"])
        self.assertEqual(self.fake.goal["status"], "paused")


class StateUpgradeTests(unittest.TestCase):
    def test_old_goal_identity_and_pause_revision_survive_additive_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("""CREATE TABLE wake_leases (
                    job_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, objective TEXT NOT NULL,
                    goal_created_at INTEGER NOT NULL, goal_updated_at INTEGER NOT NULL,
                    state TEXT NOT NULL, deadline_at REAL NOT NULL, terminal_state TEXT,
                    delivery_state TEXT NOT NULL, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                )""")
                db.execute("INSERT INTO wake_leases VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("a" * 32, str(uuid.uuid4()), "Preserve this existing Goal", 100, 103,
                            "armed", 9999999999, None, "pending", None, 100, 103))
            state = BridgeState(path)
            try:
                lease = state.get("a" * 32)
                self.assertEqual((lease.objective, lease.goal_created_at, lease.goal_updated_at),
                                 ("Preserve this existing Goal", 100, 103))
                self.assertEqual((lease.state, lease.wake_mode, lease.handoff_state),
                                 ("armed", "goal", "not_applicable"))
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
