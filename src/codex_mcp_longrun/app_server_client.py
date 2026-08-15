"""Small JSON-RPC client for the experimental Codex App Server Unix transport."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, unix_connect
from websockets.exceptions import ConnectionClosed


NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class AppServerError(RuntimeError):
    """Raised for App Server transport and JSON-RPC failures."""


class AppServerClient:
    """One initialized App Server connection with concurrent request dispatch."""

    def __init__(
        self,
        socket_path: str,
        *,
        notification_handler: NotificationHandler | None = None,
        request_timeout_sec: float = 10.0,
        max_message_bytes: int = 1 << 20,
    ) -> None:
        self.socket_path = socket_path
        self.notification_handler = notification_handler
        self.request_timeout_sec = request_timeout_sec
        self.max_message_bytes = max_message_bytes
        self._connection: ClientConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._closed_event = asyncio.Event()

    async def connect(self) -> None:
        if self._connection is not None:
            return
        self._closed_event.clear()
        try:
            self._connection = await unix_connect(
                self.socket_path,
                uri="ws://localhost/rpc",
                compression=None,
                open_timeout=self.request_timeout_sec,
                max_size=self.max_message_bytes,
            )
        except Exception as exc:
            raise AppServerError(f"cannot connect to Codex App Server: {exc}") from exc
        self._reader_task = asyncio.create_task(self._read_loop(), name="codex-app-server-reader")
        try:
            await self.call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex_longrun_bridge",
                        "title": "Codex Longrun Goal Bridge",
                        "version": "0.4.0a2",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.notify("initialized", {})
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            await connection.close()
        task = self._reader_task
        self._reader_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                await task
        self._fail_pending(AppServerError("Codex App Server connection closed"))

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        connection = self._connection
        if connection is None:
            raise AppServerError("Codex App Server is not connected")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message = json.dumps({"method": method, "id": request_id, "params": params})
        try:
            async with self._send_lock:
                await connection.send(message)
            response = await asyncio.wait_for(future, timeout=self.request_timeout_sec)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise AppServerError(f"Codex App Server request timed out: {method}") from exc
        except Exception as exc:
            self._pending.pop(request_id, None)
            if isinstance(exc, AppServerError):
                raise
            raise AppServerError(f"Codex App Server request failed: {method}: {exc}") from exc
        if "error" in response:
            raise AppServerError(f"{method}: {response['error']}")
        return response.get("result")

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        connection = self._connection
        if connection is None:
            raise AppServerError("Codex App Server is not connected")
        async with self._send_lock:
            await connection.send(json.dumps({"method": method, "params": params}))

    async def wait_closed(self) -> None:
        await self._closed_event.wait()

    async def _read_loop(self) -> None:
        assert self._connection is not None
        try:
            async for raw in self._connection:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                response_id = message.get("id")
                if isinstance(response_id, int) and "method" not in message:
                    future = self._pending.pop(response_id, None)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                method = message.get("method")
                params = message.get("params", {})
                if isinstance(method, str) and isinstance(params, dict):
                    if response_id is not None:
                        # The bridge never owns interactive approvals. Returning an
                        # explicit error is safer than leaving a request unresolved if
                        # App Server ever routes one to this non-subscribed connection.
                        await self._send_server_request_error(response_id, method)
                    elif self.notification_handler is not None:
                        await self.notification_handler(method, params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_pending(AppServerError(f"Codex App Server reader stopped: {exc}"))
        finally:
            self._closed_event.set()

    async def _send_server_request_error(self, request_id: Any, method: str) -> None:
        connection = self._connection
        if connection is None:
            return
        response = {
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"the longrun bridge cannot handle server request {method}",
            },
        }
        async with self._send_lock:
            await connection.send(json.dumps(response))

    def _fail_pending(self, error: Exception) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)
