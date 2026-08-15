"""Event-driven Goal bridge for asynchronous longrun MCP jobs."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from .app_server_client import AppServerClient, AppServerError
from .bridge_protocol import MAX_MESSAGE_BYTES, PROTOCOL_VERSION, peer_uid
from .bridge_state import BridgeState, WakeLease


JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")


class GoalBridge:
    def __init__(self, app_server_socket: Path, bridge_socket: Path, state_db: Path) -> None:
        self.app_server_socket = app_server_socket
        self.bridge_socket = bridge_socket
        self.state = BridgeState(state_db)
        self.app = AppServerClient(
            str(app_server_socket), notification_handler=self._on_notification
        )
        self.server: asyncio.AbstractServer | None = None
        self._thread_events: dict[str, asyncio.Event] = {}
        self._expected_goal_status: dict[str, str] = {}
        self._delivery_tasks: dict[str, asyncio.Task[None]] = {}
        self._deadline_tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        await self.app.connect()
        self.bridge_socket.parent.mkdir(parents=True, exist_ok=True)
        self.bridge_socket.parent.chmod(0o700)
        if self.bridge_socket.exists() or self.bridge_socket.is_symlink():
            info = self.bridge_socket.lstat()
            if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
                raise RuntimeError(f"refusing to replace unsafe bridge path: {self.bridge_socket}")
            self.bridge_socket.unlink()
        old_umask = os.umask(0o177)
        try:
            self.server = await asyncio.start_unix_server(
                self._handle_connection,
                path=str(self.bridge_socket),
                limit=MAX_MESSAGE_BYTES + 1,
            )
        finally:
            os.umask(old_umask)
        self.bridge_socket.chmod(0o600)
        for lease in self.state.pending():
            self._recover_lease(lease)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        tasks = [*self._delivery_tasks.values(), *self._deadline_tasks.values()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._delivery_tasks.clear()
        self._deadline_tasks.clear()
        await self.app.close()
        self.state.close()
        with contextlib.suppress(FileNotFoundError):
            self.bridge_socket.unlink()

    async def serve(self) -> None:
        if self.server is None:
            raise RuntimeError("bridge is not started")
        async with self.server:
            server_task = asyncio.create_task(self.server.serve_forever())
            app_closed_task = asyncio.create_task(self.app.wait_closed())
            try:
                done, _ = await asyncio.wait(
                    {server_task, app_closed_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if app_closed_task in done:
                    raise AppServerError("Codex App Server bridge connection closed")
                await server_task
            finally:
                server_task.cancel()
                app_closed_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await server_task
                with contextlib.suppress(asyncio.CancelledError):
                    await app_closed_task

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            if peer_uid(writer) != os.getuid():
                raise PermissionError("bridge accepts only same-user Unix peers")
            line = await reader.readline()
            if not line or len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                raise ValueError("invalid bridge request framing")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("bridge request must be an object")
            response = await self._dispatch(request)
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        encoded = json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        writer.write(encoded[:MAX_MESSAGE_BYTES])
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("version") != PROTOCOL_VERSION:
            raise ValueError("unsupported bridge protocol version")
        action = request.get("action")
        if action == "health":
            return {"ok": True, "version": PROTOCOL_VERSION}
        job_id = self._job_id(request.get("job_id"))
        if action == "prepare":
            return await self._prepare(job_id, request)
        if action == "terminal":
            return await self._terminal(job_id, request)
        if action == "abort":
            return await self._abort(job_id, request)
        raise ValueError(f"unsupported bridge action: {action!r}")

    async def _prepare(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._thread_id(request.get("thread_id"))
        timeout_sec = request.get("timeout_sec")
        grace_period_sec = request.get("grace_period_sec")
        if not isinstance(timeout_sec, int) or not 1 <= timeout_sec <= 7 * 24 * 60 * 60:
            raise ValueError("invalid timeout_sec")
        if not isinstance(grace_period_sec, int) or not 1 <= grace_period_sec <= 120:
            raise ValueError("invalid grace_period_sec")

        existing = self.state.get(job_id)
        if existing is not None:
            if existing.thread_id == thread_id and existing.state in {"preparing", "armed"}:
                return {"ok": True, "automatic_wakeup": True, "idempotent": True}
            raise RuntimeError("job already has a non-reusable wake lease")

        result = await self.app.call("thread/goal/get", {"threadId": thread_id})
        goal = self._goal_from_result(result)
        if goal.get("status") != "active":
            raise RuntimeError("Goal wakeup requires an active Goal")
        objective = goal.get("objective")
        created_at = goal.get("createdAt")
        updated_at = goal.get("updatedAt")
        if not isinstance(objective, str) or not isinstance(created_at, int) or not isinstance(updated_at, int):
            raise RuntimeError("App Server returned incomplete Goal identity")
        lease = self.state.create_lease(
            job_id=job_id,
            thread_id=thread_id,
            objective=objective,
            goal_created_at=created_at,
            goal_updated_at=updated_at,
            deadline_at=time.time() + timeout_sec + grace_period_sec + 60,
        )
        self._expected_goal_status[thread_id] = "paused"
        try:
            paused_result = await self.app.call(
                "thread/goal/set", {"threadId": thread_id, "status": "paused"}
            )
            paused = self._goal_from_result(paused_result)
            if not self._same_goal(lease, paused) or paused.get("status") != "paused":
                raise RuntimeError("Goal identity changed while the wake lease was being armed")
            self.state.update(
                job_id,
                state="armed",
                goal_updated_at=int(paused.get("updatedAt", updated_at)),
            )
        except Exception as exc:
            self._expected_goal_status.pop(thread_id, None)
            self.state.update(
                job_id,
                state="abandoned",
                delivery_state="abandoned",
                error=f"prepare failed: {exc}",
            )
            raise
        self._schedule_deadline(self.state.get(job_id) or lease)
        return {"ok": True, "automatic_wakeup": True, "thread_id": thread_id}

    async def _terminal(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._thread_id(request.get("thread_id"))
        terminal_state = request.get("terminal_state")
        if not isinstance(terminal_state, str) or len(terminal_state) > 64:
            raise ValueError("invalid terminal_state")
        lease = self.state.get(job_id)
        if lease is None or lease.thread_id != thread_id:
            raise RuntimeError("no matching wake lease")
        if lease.delivery_state in {"resumed", "abandoned", "needs_manual_recovery"}:
            return {"ok": True, "delivery_state": lease.delivery_state, "idempotent": True}
        lease = self.state.update(
            job_id,
            state="terminal",
            terminal_state=terminal_state,
            delivery_state="waiting_for_idle",
        )
        deadline_task = self._deadline_tasks.pop(job_id, None)
        if deadline_task is not None:
            deadline_task.cancel()
        self._schedule_delivery(lease)
        return {"ok": True, "delivery_state": "waiting_for_idle"}

    async def _abort(self, job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        thread_id = self._thread_id(request.get("thread_id"))
        lease = self.state.get(job_id)
        if lease is None:
            return {"ok": True, "delivery_state": "not_registered"}
        if lease.thread_id != thread_id:
            raise RuntimeError("wake lease belongs to another thread")
        if lease.delivery_state in {"resumed", "abandoned", "needs_manual_recovery"}:
            return {"ok": True, "delivery_state": lease.delivery_state}
        lease = self.state.update(
            job_id,
            state="terminal",
            terminal_state="bridge_aborted",
            delivery_state="waiting_for_idle",
        )
        self._schedule_delivery(lease)
        return {"ok": True, "delivery_state": "waiting_for_idle"}

    async def _deliver(self, lease: WakeLease) -> None:
        try:
            await self._wait_until_idle(lease.thread_id)
            current = self.state.get(lease.job_id)
            if current is None or current.delivery_state in {
                "resumed",
                "abandoned",
                "needs_manual_recovery",
            }:
                return
            result = await self.app.call("thread/goal/get", {"threadId": lease.thread_id})
            goal = self._goal_from_result(result)
            if not self._same_goal(lease, goal):
                self.state.update(
                    lease.job_id,
                    state="abandoned",
                    delivery_state="abandoned",
                    error="Goal identity changed before terminal delivery",
                )
                return
            status = goal.get("status")
            if status == "active":
                self.state.update(
                    lease.job_id,
                    state="abandoned",
                    delivery_state="abandoned",
                    error="Goal was resumed outside the bridge",
                )
                return
            if status != "paused":
                self.state.update(
                    lease.job_id,
                    state="abandoned",
                    delivery_state="abandoned",
                    error=f"Goal status changed to {status!r}",
                )
                return
            self.state.update(lease.job_id, delivery_state="activating")
            self._expected_goal_status[lease.thread_id] = "active"
            try:
                activated_result = await self.app.call(
                    "thread/goal/set", {"threadId": lease.thread_id, "status": "active"}
                )
            except Exception as exc:
                self._expected_goal_status.pop(lease.thread_id, None)
                # An interrupted status-changing request is ambiguous. Never retry
                # it automatically; a duplicate active transition could start an
                # extra Goal turn.
                self.state.update(
                    lease.job_id,
                    state="failed",
                    delivery_state="needs_manual_recovery",
                    error=f"ambiguous Goal activation: {exc}",
                )
                return
            activated = self._goal_from_result(activated_result)
            if not self._same_goal(lease, activated) or activated.get("status") != "active":
                self.state.update(
                    lease.job_id,
                    state="failed",
                    delivery_state="needs_manual_recovery",
                    error="App Server did not confirm Goal activation",
                )
                return
            self.state.update(lease.job_id, state="delivered", delivery_state="resumed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current = self.state.get(lease.job_id)
            if current is not None and current.delivery_state not in {"resumed", "abandoned"}:
                self.state.update(
                    lease.job_id,
                    state="failed",
                    delivery_state="needs_manual_recovery",
                    error=f"delivery failed: {type(exc).__name__}: {exc}",
                )
        finally:
            self._delivery_tasks.pop(lease.job_id, None)

    async def _wait_until_idle(self, thread_id: str) -> None:
        event = self._thread_events.setdefault(thread_id, asyncio.Event())
        while True:
            result = await self.app.call(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
            status = self._thread_status(result)
            if status == "idle":
                return
            if status == "systemError":
                raise RuntimeError("thread entered systemError before Goal wakeup")
            event.clear()
            # Close the notification race between the first status read and
            # clearing the event without introducing periodic polling.
            result = await self.app.call(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
            status = self._thread_status(result)
            if status == "idle":
                return
            if status == "systemError":
                raise RuntimeError("thread entered systemError before Goal wakeup")
            await event.wait()

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "thread/status/changed":
            thread_id = params.get("threadId")
            if isinstance(thread_id, str):
                self._thread_events.setdefault(thread_id, asyncio.Event()).set()
            return
        if method == "turn/completed":
            thread_id = params.get("threadId")
            if isinstance(thread_id, str):
                self._thread_events.setdefault(thread_id, asyncio.Event()).set()
            return
        if method == "thread/goal/cleared":
            thread_id = params.get("threadId")
            if isinstance(thread_id, str):
                self._abandon_for_manual_change(thread_id, "Goal was cleared")
            return
        if method != "thread/goal/updated":
            return
        thread_id = params.get("threadId")
        goal = params.get("goal")
        if not isinstance(thread_id, str) or not isinstance(goal, dict):
            return
        expected = self._expected_goal_status.get(thread_id)
        if expected is not None:
            if goal.get("status") == expected:
                self._expected_goal_status.pop(thread_id, None)
            return
        lease = self.state.live_for_thread(thread_id)
        if lease is None:
            return
        if not self._same_goal(lease, goal) or goal.get("status") != "paused":
            self._abandon_for_manual_change(thread_id, "Goal was changed outside the bridge")

    def _abandon_for_manual_change(self, thread_id: str, reason: str) -> None:
        lease = self.state.live_for_thread(thread_id)
        if lease is None or lease.delivery_state == "activating":
            return
        self.state.update(
            lease.job_id,
            state="abandoned",
            delivery_state="abandoned",
            error=reason,
        )
        task = self._delivery_tasks.pop(lease.job_id, None)
        if task is not None:
            task.cancel()
        deadline = self._deadline_tasks.pop(lease.job_id, None)
        if deadline is not None:
            deadline.cancel()

    def _recover_lease(self, lease: WakeLease) -> None:
        if lease.state == "terminal":
            self._schedule_delivery(lease)
        elif lease.state == "armed":
            self._schedule_deadline(lease)
        else:
            self.state.update(
                lease.job_id,
                state="failed",
                delivery_state="needs_manual_recovery",
                error="bridge restarted during wake lease preparation",
            )

    def _schedule_delivery(self, lease: WakeLease) -> None:
        existing = self._delivery_tasks.get(lease.job_id)
        if existing is None or existing.done():
            self._delivery_tasks[lease.job_id] = asyncio.create_task(
                self._deliver(lease), name=f"longrun-wake-{lease.job_id}"
            )

    def _schedule_deadline(self, lease: WakeLease) -> None:
        existing = self._deadline_tasks.get(lease.job_id)
        if existing is None or existing.done():
            self._deadline_tasks[lease.job_id] = asyncio.create_task(
                self._expire_at_deadline(lease), name=f"longrun-deadline-{lease.job_id}"
            )

    async def _expire_at_deadline(self, lease: WakeLease) -> None:
        try:
            await asyncio.sleep(max(0.0, lease.deadline_at - time.time()))
            current = self.state.get(lease.job_id)
            if current is None or current.state != "armed":
                return
            current = self.state.update(
                lease.job_id,
                state="terminal",
                terminal_state="bridge_deadline_expired",
                delivery_state="waiting_for_idle",
                error="MCP server did not deliver a terminal event before the guarded deadline",
            )
            self._schedule_delivery(current)
        finally:
            self._deadline_tasks.pop(lease.job_id, None)

    @staticmethod
    def _job_id(value: Any) -> str:
        if not isinstance(value, str) or not JOB_ID_RE.fullmatch(value):
            raise ValueError("invalid job_id")
        return value

    @staticmethod
    def _thread_id(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("invalid thread_id")
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("invalid thread_id") from exc
        if str(parsed) != value.lower():
            raise ValueError("thread_id must use canonical UUID syntax")
        return value.lower()

    @staticmethod
    def _goal_from_result(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict) or not isinstance(result.get("goal"), dict):
            raise RuntimeError("thread has no durable Goal")
        return result["goal"]

    @staticmethod
    def _same_goal(lease: WakeLease, goal: dict[str, Any]) -> bool:
        return (
            goal.get("threadId") == lease.thread_id
            and goal.get("createdAt") == lease.goal_created_at
            and goal.get("objective") == lease.objective
        )

    @staticmethod
    def _thread_status(result: Any) -> str:
        if not isinstance(result, dict):
            raise RuntimeError("invalid thread/read response")
        thread = result.get("thread")
        status = thread.get("status") if isinstance(thread, dict) else None
        status_type = status.get("type") if isinstance(status, dict) else None
        if not isinstance(status_type, str):
            raise RuntimeError("thread/read response has no runtime status")
        return status_type


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge longrun terminal events to Codex Goal wakeups")
    parser.add_argument("--app-server-socket", required=True, type=Path)
    parser.add_argument("--bridge-socket", required=True, type=Path)
    parser.add_argument("--state-db", required=True, type=Path)
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> None:
    bridge = GoalBridge(
        args.app_server_socket.expanduser().resolve(),
        args.bridge_socket.expanduser().resolve(),
        args.state_db.expanduser().resolve(),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    await bridge.start()
    serve_task = asyncio.create_task(bridge.serve())
    stop_task = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if serve_task in done:
            await serve_task
    finally:
        serve_task.cancel()
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        await bridge.close()


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except (AppServerError, RuntimeError, OSError) as exc:
        raise SystemExit(f"codex-longrun-bridge: {exc}") from exc


if __name__ == "__main__":
    main()
