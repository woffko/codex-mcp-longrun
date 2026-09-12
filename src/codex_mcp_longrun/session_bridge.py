"""Session handoff layered on the unchanged durable Goal bridge.

The MCP call stays pending until its originating turn has been interrupted.
A durable receipt replaces reliance on the interrupted outer code-mode result.
All session control uses the owning TUI connection so approvals stay in the UI.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .bridge import GoalBridge
from .bridge_state import WakeLease


class SessionPeer(Protocol):
    async def call(self, method: str, params: dict[str, Any]) -> Any: ...
    async def exclusive(self, operation: Callable[[], Awaitable[Any]]) -> Any: ...


class SessionBridge(GoalBridge):
    """A coordinator-owned bridge with an authenticated local TUI event stream."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.peers: dict[str, SessionPeer] = {}
        self.lease_peers: dict[str, SessionPeer] = {}
        self.ambiguous_threads: set[str] = set()
        self.calls: OrderedDict[tuple[str, str], tuple[str, SessionPeer]] = OrderedDict()
        # Only live notifications on the exact owning connection are authority.
        # A paginated legacy-history request may still scan gigabytes on disk.
        self.observed_turns: dict[str, tuple[SessionPeer, str, str]] = {}
        self.call_changed = asyncio.Event()
        self.turn_done: dict[tuple[str, str], asyncio.Event] = {}

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        # Let the base dispatcher validate protocol versions for every action.
        from .bridge_protocol import PROTOCOL_VERSION
        if request.get("version") != PROTOCOL_VERSION:
            raise ValueError("unsupported bridge protocol version")
        if request.get("action") == "health":
            return {"ok": True, "version": PROTOCOL_VERSION,
                    "session_wakeup_supported": True,
                    "session_history_free": True,
                    "state_based_routing": True,
                    "session_transport_ready": bool(self.peers)}
        if request.get("action") in {"handoff", "cancel_wakeup", "wake_status"}:
            job_id = self._job_id(request.get("job_id"))
            thread_id = self._thread_id(request.get("thread_id"))
            lease = self.state.get(job_id)
            if lease is None or lease.thread_id != thread_id:
                raise RuntimeError("no matching wake lease")
            if lease.wake_mode != "session":
                raise RuntimeError("session control cannot change a Goal lease")
            if request["action"] == "wake_status":
                return {"ok": True, "delivery_state": lease.delivery_state, "error": lease.error}
            if request["action"] == "cancel_wakeup":
                if lease.state not in {"preparing", "armed", "terminal", "abandoned"}:
                    return {"ok": True, "wakeup_cancelled": False, "job_cancelled": False,
                            "delivery_state": lease.delivery_state}
                self._abandon_for_manual_change(thread_id, "Session wakeup explicitly cancelled")
                return {"ok": True, "wakeup_cancelled": True, "job_cancelled": False}
            return await self._handoff(lease, request)
        return await super()._dispatch(request)

    async def observe(self, peer: SessionPeer, method: str, params: dict[str, Any]) -> None:
        """Accept only events from the TUI connection, never model-supplied IDs."""
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return
        if method in {"item/started", "turn/started", "turn/completed"}:
            owner = self.peers.get(thread_id)
            if owner is not None and owner is not peer:
                # Multiple subscribed TUI clients receive the same tool events.
                # Do not guess which client owns a call or redirect its approvals.
                self.ambiguous_threads.add(thread_id)
                self.user_activity(thread_id)
                return
        if method == "item/started":
            item = params.get("item", {})
            if (isinstance(item, dict) and item.get("type") == "mcpToolCall"
                    and item.get("server") == "longrun" and item.get("tool") == "start_job"
                    and isinstance(item.get("id"), str) and isinstance(params.get("turnId"), str)):
                turn_id = params["turnId"]
                if not turn_id or len(turn_id) > 256:
                    return
                observed_turn = self.observed_turns.get(thread_id)
                if observed_turn is None:
                    # A TUI resumed during a live turn need not have seen its
                    # turn/started notification. Its new live MCP item is a
                    # sufficient seed; persisted/replayed history never is.
                    self.observed_turns[thread_id] = (peer, turn_id, "inProgress")
                elif observed_turn != (peer, turn_id, "inProgress"):
                    # Do not revive a completed turn or adopt a stale item
                    # belonging to a previously observed turn.
                    return
                self.calls[(thread_id, item["id"])] = (params["turnId"], peer)
                self.peers[thread_id] = peer
                while len(self.calls) > 256:
                    self.calls.popitem(last=False)
                self.call_changed.set()
        if method == "turn/started":
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 256:
                self.user_activity(thread_id)
                return
            previous = self.observed_turns.get(thread_id)
            if previous is not None and previous[1] == turn_id and previous[2] != "inProgress":
                return  # An out-of-order duplicate cannot reopen a terminal turn.
            self.peers[thread_id] = peer
            self.observed_turns[thread_id] = (peer, turn_id, "inProgress")
            lease = self.state.live_for_thread(thread_id)
            if (lease and lease.wake_mode == "session" and turn_id != lease.turn_id
                    and lease.delivery_state != "activating"):
                self._abandon_for_manual_change(thread_id, "Another turn started before wake delivery")
        if method == "turn/completed":
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if isinstance(turn_id, str):
                previous = self.observed_turns.get(thread_id)
                if previous is not None and previous[1] != turn_id:
                    return  # Completion of an older turn cannot release this handoff.
                if previous is not None and previous[2] != "inProgress":
                    return  # Terminal state is immutable, including duplicate events.
                status = turn.get("status")
                if status not in {"completed", "interrupted", "failed"}:
                    status = "unknown"
                self.peers[thread_id] = peer
                self.observed_turns[thread_id] = (peer, turn_id, status)
                event = self.turn_done.get((thread_id, turn_id))
                if event is not None:
                    event.set()
                lease = self.state.live_for_thread(thread_id)
                if (lease and lease.wake_mode == "session" and turn_id == lease.turn_id
                        and lease.handoff_state not in {"interrupting", "ready"}):
                    self._abandon_for_manual_change(thread_id, "Originating turn ended before handoff")
                for key, (call_turn, _) in list(self.calls.items()):
                    if key[0] == thread_id and call_turn == turn_id:
                        self.calls.pop(key, None)
        if method in {"turn/completed", "thread/status/changed"}:
            self._thread_events.setdefault(thread_id, asyncio.Event()).set()

    def user_activity(self, thread_id: str) -> None:
        for key in list(self.calls):
            if key[0] == thread_id:
                del self.calls[key]
        lease = self.state.live_for_thread(thread_id)
        if lease and lease.wake_mode == "session":
            self._abandon_for_manual_change(thread_id, "User activity superseded automatic continuation")

    def disconnected(self, peer: SessionPeer) -> None:
        for thread_id, owner in list(self.peers.items()):
            if owner is peer:
                self.user_activity(thread_id)
                del self.peers[thread_id]
                self.observed_turns.pop(thread_id, None)
                self.ambiguous_threads.discard(thread_id)
        for job_id, owner in list(self.lease_peers.items()):
            if owner is peer:
                lease = self.state.get(job_id)
                if lease is not None:
                    self.user_activity(lease.thread_id)
                self.lease_peers.pop(job_id, None)
        for key, (_, owner) in list(self.calls.items()):
            if owner is peer:
                del self.calls[key]

    async def _latest_turn(self, peer: SessionPeer, thread_id: str) -> dict[str, Any]:
        # Keep the await-compatible helper for the three guarded call sites,
        # but never query or reconstruct persisted history here.
        observed = self.observed_turns.get(thread_id)
        if (observed is None or observed[0] is not peer
                or self.peers.get(thread_id) is not peer
                or thread_id in self.ambiguous_threads):
            raise RuntimeError("cannot confirm the current turn from its owning live event stream")
        return {"id": observed[1], "status": observed[2]}

    async def _prepare(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        policy = request.get("wake_policy", "goal")
        if policy not in {"auto", "goal", "session"}:
            raise ValueError("invalid wake policy")
        thread_id = self._thread_id(request.get("thread_id"))
        self._prepare_stage(job_id, "routing/read-goal")
        result = await self.app.call("thread/goal/get", {"threadId": thread_id})
        if not isinstance(result, dict) or "goal" not in result:
            raise RuntimeError("cannot establish Goal state")
        goal = result["goal"]
        if isinstance(goal, dict) and goal.get("status") == "active":
            result = await super()._prepare(job_id, request)
            return {**result, "wake_mode": "goal", "state_based_routing": True}
        if goal is not None and (not isinstance(goal, dict) or goal.get("status") not in {"paused", "blocked", "complete"}):
            raise RuntimeError("state-based routing cannot bypass Goal usage/budget limits or an unknown Goal state")
        call_id = request.get("call_id")
        if not isinstance(call_id, str) or not 1 <= len(call_id) <= 256:
            raise RuntimeError("session wakeup requires trusted MCP call metadata")
        timeout = request.get("timeout_sec")
        grace = request.get("grace_period_sec")
        if (type(timeout) is not int or not 1 <= timeout <= 7 * 24 * 3600
                or type(grace) is not int or not 1 <= grace <= 120):
            raise ValueError("invalid session wake deadline")
        key = (thread_id, call_id)
        # The TUI event and the MCP request travel over different local sockets.
        async def observed() -> tuple[str, SessionPeer]:
            while key not in self.calls:
                self.call_changed.clear()
                if key in self.calls:
                    break
                await self.call_changed.wait()
            return self.calls[key]
        try:
            self._prepare_stage(job_id, "session/observe-call")
            turn_id, peer = await asyncio.wait_for(observed(), 2)
        except TimeoutError as exc:
            raise RuntimeError("session handoff requires the matching live codex-longrun TUI connection") from exc

        existing = self.state.get(job_id)
        if existing is not None:
            if (existing.thread_id == thread_id and existing.wake_mode == "session"
                    and existing.state == "armed" and existing.handoff_state == "pending"
                    and existing.delivery_state == "pending"
                    and existing.objective == json.dumps(goal, sort_keys=True)
                    and existing.turn_id == turn_id and existing.call_id == call_id
                    and self.lease_peers.get(job_id) is peer
                    and self.peers.get(thread_id) is peer
                    and thread_id not in self.ambiguous_threads
                    and self.calls.get(key) == (turn_id, peer)):
                return {"ok": True, "automatic_wakeup": True, "wake_mode": "session",
                        "state_based_routing": True, "thread_id": thread_id,
                        "turn_id": turn_id, "idempotent": True}
            raise RuntimeError("job already has a non-reusable wake lease")
        lease = self.state.create_lease(
            job_id=job_id, thread_id=thread_id,
            objective=json.dumps(goal, sort_keys=True), goal_created_at=0, goal_updated_at=0,
            deadline_at=time.time() + timeout + grace + 60,
            wake_mode="session", turn_id=turn_id, call_id=call_id,
        )

        async def prepare() -> dict[str, Any]:
            if thread_id in self.ambiguous_threads:
                raise RuntimeError("session handoff requires one unambiguous owning TUI connection")
            if self.calls.get(key) != (turn_id, peer):
                raise RuntimeError("user activity superseded the originating tool call")
            self._prepare_stage(job_id, "session/check-live-turn")
            latest = await self._latest_turn(peer, thread_id)
            if latest.get("id") != turn_id or latest.get("status") != "inProgress":
                raise RuntimeError("originating tool call is no longer in the active turn")
            self._prepare_stage(job_id, "session/confirm-goal")
            goal_now = await peer.call("thread/goal/get", {"threadId": thread_id})
            if not isinstance(goal_now, dict) or "goal" not in goal_now or goal_now["goal"] != goal:
                raise RuntimeError("Goal changed before session registration")
            latest = await self._latest_turn(peer, thread_id)
            if latest.get("id") != turn_id or latest.get("status") != "inProgress":
                raise RuntimeError("originating turn changed during session registration")
            current = self.state.get(job_id)
            if (current is None or current.state != "preparing" or current.delivery_state != "pending"
                    or current.wake_mode != "session" or current.turn_id != turn_id
                    or current.call_id != call_id):
                raise RuntimeError("session wake registration was revoked before confirmation")
            self.state.update(job_id, state="armed")
            self.lease_peers[job_id] = peer
            self._schedule_deadline(self.state.get(job_id) or lease)
            return {"ok": True, "automatic_wakeup": True, "wake_mode": "session",
                    "state_based_routing": True,
                    "thread_id": thread_id, "turn_id": turn_id}
        try:
            self._prepare_stage(job_id, "session/wait-for-owner")
            return await peer.exclusive(prepare)
        except (Exception, asyncio.CancelledError) as exc:
            current = self.state.get(job_id)
            if current is not None and current.state == "preparing":
                self.state.update(job_id, state="abandoned", delivery_state="abandoned",
                                  error=f"session prepare failed: {type(exc).__name__}: {exc}")
            self.lease_peers.pop(job_id, None)
            raise

    async def _abort(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        lease = self.state.get(job_id)
        if lease is None or lease.wake_mode != "session":
            return await super()._abort(job_id, request)
        thread_id = self._thread_id(request.get("thread_id"))
        self.state.abort_registration(job_id, thread_id)
        if lease.thread_id != thread_id:
            raise RuntimeError("wake lease belongs to another thread")
        if lease.delivery_state in {"resumed", "abandoned", "needs_manual_recovery"}:
            return {"ok": True, "delivery_state": lease.delivery_state}
        self._abandon_for_manual_change(
            thread_id, "Session wake registration aborted before command start"
        )
        self.lease_peers.pop(job_id, None)
        return {"ok": True, "delivery_state": "abandoned"}

    def _live_session(self, job_id: str) -> WakeLease:
        lease = self.state.get(job_id)
        if (lease is None or lease.wake_mode != "session" or lease.state not in {"armed", "terminal"}
                or lease.delivery_state in {"abandoned", "needs_manual_recovery", "resumed"}):
            raise RuntimeError("session wake lease is no longer live")
        return lease

    def _owner(self, lease: WakeLease) -> SessionPeer:
        peer = self.lease_peers.get(lease.job_id)
        if (peer is None or self.peers.get(lease.thread_id) is not peer
                or lease.thread_id in self.ambiguous_threads):
            raise RuntimeError("the original owning TUI is unavailable or ambiguous")
        return peer

    async def _check_goal(self, lease: WakeLease, peer: SessionPeer) -> None:
        result = await peer.call("thread/goal/get", {"threadId": lease.thread_id})
        if not isinstance(result, dict) or "goal" not in result:
            raise RuntimeError("cannot confirm unchanged Goal state")
        if json.dumps(result["goal"], sort_keys=True) != lease.objective:
            raise RuntimeError("Goal changed while session continuation was pending")

    async def _handoff(self, lease: WakeLease, request: dict[str, Any]) -> dict[str, Any]:
        state = request.get("job_state")
        if state not in {"starting", "running", "succeeded", "failed", "timed_out", "inactive_timeout",
                         "cancelled", "spawn_error", "success_condition_not_met", "orphaned_recovered"}:
            raise ValueError("invalid session start receipt")
        lease = self._live_session(lease.job_id)
        if lease.handoff_state == "ready":
            return {"ok": True, "handoff": "complete", "idempotent": True}
        peer = self._owner(lease)

        async def transfer() -> dict[str, Any]:
            current = self._live_session(lease.job_id)
            if current.handoff_state != "pending":
                raise RuntimeError("handoff already attempted; do not retry interruption")
            await self._check_goal(current, peer)
            latest = await self._latest_turn(peer, lease.thread_id)
            if latest.get("id") != lease.turn_id or latest.get("status") != "inProgress":
                raise RuntimeError("originating turn changed before handoff")
            self._live_session(lease.job_id)
            self._owner(lease)
            done = self.turn_done.setdefault((lease.thread_id, lease.turn_id or ""), asyncio.Event())
            self.state.update(lease.job_id, handoff_state="interrupting", receipt_state=state)
            try:
                await peer.call("turn/interrupt", {"threadId": lease.thread_id, "turnId": lease.turn_id})
                await asyncio.wait_for(done.wait(), 5)
                latest = await self._latest_turn(peer, lease.thread_id)
                if latest.get("id") != lease.turn_id or latest.get("status") != "interrupted":
                    raise RuntimeError("App Server did not confirm interruption of the originating turn")
                self._live_session(lease.job_id)
                self.state.update(lease.job_id, handoff_state="recording_receipt")
                receipt = (
                    f"[Longrun session handoff]\nJob {lease.job_id} was registered for automatic "
                    f"session continuation; its state at handoff was {state}. The bridge intentionally "
                    "interrupted the pending start_job transport after startup to release the model turn. "
                    "An aborted outer functions.exec does not mean the background job failed to start. "
                    "Do not launch a duplicate. On the completion wake, call longrun.get_job once for "
                    "this job, inspect its result, and continue the original user task. "
                    "No Goal was created or reactivated. New user instructions take precedence."
                )
                await peer.call("thread/inject_items", {"threadId": lease.thread_id, "items": [
                    {"type": "message", "role": "developer", "content": [
                        {"type": "input_text", "text": receipt}]}]})
                current = self._live_session(lease.job_id)
                self.state.update(lease.job_id, handoff_state="ready")
                if current.state == "terminal":
                    self._schedule_delivery(self.state.get(lease.job_id) or current)
                return {"ok": True, "handoff": "complete"}
            finally:
                self.turn_done.pop((lease.thread_id, lease.turn_id or ""), None)
        try:
            return await asyncio.wait_for(peer.exclusive(transfer), 12)
        except Exception as exc:
            self._session_failed(lease.job_id, f"handoff failed: {exc}")
            raise

    async def _terminal(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        lease = self.state.get(job_id)
        if lease is None or lease.wake_mode != "session":
            return await super()._terminal(job_id, request)
        if lease.thread_id != self._thread_id(request.get("thread_id")):
            raise RuntimeError("wake lease belongs to another thread")
        if lease.state not in {"armed", "terminal"}:
            return {"ok": True, "delivery_state": lease.delivery_state}
        terminal = request.get("terminal_state")
        if not isinstance(terminal, str) or len(terminal) > 64:
            raise ValueError("invalid terminal_state")
        if lease.state != "terminal":
            lease = self.state.update(job_id, state="terminal", terminal_state=terminal,
                                      delivery_state="waiting_for_handoff")
        if lease.handoff_state == "ready":
            self._schedule_delivery(lease)
        return {"ok": True, "delivery_state": lease.delivery_state}

    async def _abort(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        lease = self.state.get(job_id)
        if lease and lease.wake_mode == "session":
            if lease.thread_id != self._thread_id(request.get("thread_id")):
                raise RuntimeError("wake lease belongs to another thread")
            self._abandon_for_manual_change(lease.thread_id, "Session registration aborted")
            return {"ok": True, "delivery_state": "abandoned"}
        return await super()._abort(job_id, request)

    async def _deliver(self, lease: WakeLease) -> None:
        if lease.wake_mode != "session":
            return await super()._deliver(lease)
        try:
            peer = self._owner(lease)
            async def wake() -> None:
                current = self._live_session(lease.job_id)
                if current.state != "terminal" or current.handoff_state != "ready":
                    return
                await self._check_goal(current, peer)
                latest = await self._latest_turn(peer, lease.thread_id)
                if latest.get("id") != lease.turn_id or latest.get("status") != "interrupted":
                    raise RuntimeError("newer user activity superseded this wakeup")
                self._live_session(lease.job_id)
                self._owner(lease)
                # Commit before the non-idempotent RPC; ambiguous replies never retry.
                self.state.update(lease.job_id, delivery_state="activating")
                result = await peer.call("turn/start", {"threadId": lease.thread_id, "input": [
                    {"type": "text", "text": (
                        f"[Longrun session wake]\nJob {lease.job_id} reached terminal state "
                        f"{current.terminal_state}. Read its result once with longrun.get_job, "
                        "then continue the original task under the latest user instructions. "
                        "This is one job completion notification, not a new Goal. Do not restart the job."
                    )}]})
                turn_id = result.get("turn", {}).get("id") if isinstance(result, dict) else None
                if not isinstance(turn_id, str):
                    raise RuntimeError("App Server did not confirm the continuation turn")
                self.state.update(lease.job_id, state="delivered", delivery_state="resumed", wake_turn_id=turn_id)
                task = self._deadline_tasks.pop(lease.job_id, None)
                if task:
                    task.cancel()
            await peer.exclusive(wake)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._session_failed(lease.job_id, f"session delivery failed: {exc}")
        finally:
            self._delivery_tasks.pop(lease.job_id, None)
            current = self.state.get(lease.job_id)
            if current and current.state not in {"armed", "terminal"}:
                self.lease_peers.pop(lease.job_id, None)

    def _session_failed(self, job_id: str, reason: str) -> None:
        lease = self.state.get(job_id)
        if lease and lease.state in {"armed", "terminal"}:
            self.state.update(job_id, state="failed", delivery_state="needs_manual_recovery", error=reason)
            self.lease_peers.pop(job_id, None)

    def _abandon_for_manual_change(self, thread_id: str, reason: str) -> None:
        lease = self.state.live_for_thread(thread_id)
        super()._abandon_for_manual_change(thread_id, reason)
        if lease and lease.wake_mode == "session":
            current = self.state.get(lease.job_id)
            if current and current.state == "abandoned":
                self.lease_peers.pop(lease.job_id, None)

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        lease = self.state.live_for_thread(thread_id) if isinstance(thread_id, str) else None
        if lease and lease.wake_mode == "session" and method in {"thread/goal/updated", "thread/goal/cleared"}:
            goal = params.get("goal") if method == "thread/goal/updated" else None
            if json.dumps(goal, sort_keys=True) != lease.objective:
                self._abandon_for_manual_change(thread_id, "Goal changed while session wakeup was pending")
            return
        await super()._on_notification(method, params)

    def _recover_lease(self, lease: WakeLease) -> None:
        if lease.wake_mode == "session":
            # The launcher tears down the MCP host with the coordinator. An old
            # UI ownership/interrupt/wake acknowledgement cannot be reconstructed.
            self.state.update(lease.job_id, state="failed", delivery_state="needs_manual_recovery",
                              error="coordinator restarted; session ownership must be re-established manually")
            return
        super()._recover_lease(lease)

    async def _expire_at_deadline(self, lease: WakeLease) -> None:
        if lease.wake_mode != "session":
            return await super()._expire_at_deadline(lease)
        try:
            await asyncio.sleep(max(0, lease.deadline_at - time.time()))
            self._session_failed(lease.job_id, "session completion or handoff acknowledgement deadline expired")
        finally:
            self._deadline_tasks.pop(lease.job_id, None)
