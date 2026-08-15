"""Bounded compatibility proxy for the official Codex remote TUI."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import os
import socket
import stat
import struct
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from websockets.asyncio.client import ClientConnection, unix_connect
from websockets.asyncio.server import Server, ServerConnection, unix_serve
from websockets.exceptions import ConnectionClosed

from .app_server_client import AppServerClient, AppServerError


DEFAULT_HISTORY_THRESHOLD_BYTES = 64 << 20
DEFAULT_HISTORY_TAIL_TURNS = 5
DEFAULT_HELPER_TIMEOUT_SEC = 120.0
HELPER_MAX_MESSAGE_BYTES = 16 << 20
PROXY_MAX_MESSAGE_BYTES = 128 << 20
VALID_MODES = {"auto", "omit", "off"}


JsonObject = dict[str, Any]
JsonId = int | str
SendUpstream = Callable[[str | bytes], Awaitable[None]]
SendClient = Callable[[str | bytes | JsonObject], Awaitable[None]]


def _json_id(message: JsonObject) -> JsonId | None:
    value = message.get("id")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str)):
        return value
    return None


def _decode_object(raw: str | bytes) -> JsonObject | None:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _thread_from_result(result: Any) -> JsonObject | None:
    if not isinstance(result, dict):
        return None
    thread = result.get("thread")
    return thread if isinstance(thread, dict) else None


def _thread_id(thread: JsonObject) -> str | None:
    value = thread.get("id")
    return value if isinstance(value, str) and value else None


def _history_mode(thread: JsonObject) -> str | None:
    value = thread.get("historyMode", thread.get("history_mode"))
    return value.lower() if isinstance(value, str) else None


def _rollout_path(thread: JsonObject) -> Path | None:
    value: Any = thread.get("path", thread.get("rolloutPath"))
    if isinstance(value, dict):
        value = value.get("path", value.get("value"))
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return None
    return candidate


def _rollout_size(thread: JsonObject) -> int | None:
    path = _rollout_path(thread)
    if path is None:
        return None
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def _peer_uid(connection: ServerConnection) -> int | None:
    transport_socket = connection.transport.get_extra_info("socket")
    if transport_socket is None or not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        credentials = transport_socket.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


class TuiCompatibilityProxy:
    """Relay TUI JSON-RPC while bounding large Legacy history reads."""

    def __init__(
        self,
        app_server_socket: Path,
        tui_socket: Path,
        *,
        legacy_history_mode: str = "auto",
        history_threshold_bytes: int = DEFAULT_HISTORY_THRESHOLD_BYTES,
        history_tail_turns: int = DEFAULT_HISTORY_TAIL_TURNS,
        helper_timeout_sec: float = DEFAULT_HELPER_TIMEOUT_SEC,
        helper_max_message_bytes: int = HELPER_MAX_MESSAGE_BYTES,
        proxy_max_message_bytes: int = PROXY_MAX_MESSAGE_BYTES,
    ) -> None:
        if legacy_history_mode not in VALID_MODES:
            raise ValueError(f"unsupported Legacy history mode: {legacy_history_mode}")
        if history_threshold_bytes <= 0:
            raise ValueError("history threshold must be positive")
        if history_tail_turns <= 0:
            raise ValueError("history tail size must be positive")
        self.app_server_socket = app_server_socket
        self.tui_socket = tui_socket
        self.legacy_history_mode = legacy_history_mode
        self.history_threshold_bytes = history_threshold_bytes
        self.history_tail_turns = history_tail_turns
        self.helper_timeout_sec = helper_timeout_sec
        self.helper_max_message_bytes = helper_max_message_bytes
        self.proxy_max_message_bytes = proxy_max_message_bytes
        self.server: Server | None = None

    async def start(self) -> None:
        self.tui_socket.parent.mkdir(parents=True, exist_ok=True)
        self.tui_socket.parent.chmod(0o700)
        if self.tui_socket.exists() or self.tui_socket.is_symlink():
            info = self.tui_socket.lstat()
            if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
                raise RuntimeError(f"refusing to replace unsafe TUI proxy path: {self.tui_socket}")
            self.tui_socket.unlink()
        old_umask = os.umask(0o177)
        try:
            self.server = await unix_serve(
                self._handle_client,
                str(self.tui_socket),
                compression=None,
                max_size=self.proxy_max_message_bytes,
            )
        finally:
            os.umask(old_umask)
        self.tui_socket.chmod(0o600)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        with contextlib.suppress(FileNotFoundError):
            self.tui_socket.unlink()

    async def serve(self) -> None:
        if self.server is None:
            raise RuntimeError("TUI compatibility proxy is not started")
        await self.server.serve_forever()

    async def _handle_client(self, client: ServerConnection) -> None:
        uid = _peer_uid(client)
        if uid is not None and uid != os.getuid():
            await client.close(code=1008, reason="same-user Unix peers only")
            return
        try:
            upstream = await unix_connect(
                str(self.app_server_socket),
                uri="ws://localhost/rpc",
                compression=None,
                open_timeout=10,
                max_size=self.proxy_max_message_bytes,
            )
        except Exception as exc:
            print(
                "codex-longrun history proxy: App Server connection failed: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            await client.close(code=1011, reason="App Server connection failed")
            return

        summaries: dict[str, JsonObject] = {}
        pending_summary_reads: dict[JsonId, str] = {}
        history_locks: dict[str, asyncio.Lock] = {}
        tail_cache: dict[str, list[Any]] = {}
        upstream_send_lock = asyncio.Lock()
        client_send_lock = asyncio.Lock()
        intercepted: set[asyncio.Task[None]] = set()

        async def send_upstream(raw: str | bytes) -> None:
            async with upstream_send_lock:
                await upstream.send(raw)

        async def send_client(message: str | bytes | JsonObject) -> None:
            raw = (
                json.dumps(message, separators=(",", ":"), ensure_ascii=True)
                if isinstance(message, dict)
                else message
            )
            async with client_send_lock:
                await client.send(raw)

        async def client_to_upstream() -> None:
            async for raw in client:
                message = _decode_object(raw)
                if message is None or message.get("method") != "thread/read":
                    await send_upstream(raw)
                    continue
                request_id = _json_id(message)
                params = message.get("params")
                if request_id is None or not isinstance(params, dict):
                    await send_upstream(raw)
                    continue
                requested_thread_id = params.get("threadId")
                if not isinstance(requested_thread_id, str) or not requested_thread_id:
                    await send_upstream(raw)
                    continue
                if params.get("includeTurns") is not True:
                    pending_summary_reads[request_id] = requested_thread_id
                    await send_upstream(raw)
                    continue
                if self.legacy_history_mode == "off":
                    await send_upstream(raw)
                    continue
                async def guarded_full_read(
                    thread_id: str = requested_thread_id,
                    read_message: JsonObject = message,
                    read_raw: str | bytes = raw,
                ) -> None:
                    lock = history_locks.setdefault(thread_id, asyncio.Lock())
                    async with lock:
                        await self._handle_full_read(
                            request=read_message,
                            raw_request=read_raw,
                            summaries=summaries,
                            tail_cache=tail_cache,
                            send_upstream=send_upstream,
                            send_client=send_client,
                        )

                task = asyncio.create_task(
                    guarded_full_read(),
                    name=f"codex-longrun-history-{requested_thread_id}",
                )
                intercepted.add(task)
                task.add_done_callback(intercepted.discard)

        async def upstream_to_client() -> None:
            async for raw in upstream:
                message = _decode_object(raw)
                if message is not None and message.get("method") in {
                    "turn/started",
                    "turn/completed",
                }:
                    params = message.get("params")
                    changed_thread_id = params.get("threadId") if isinstance(params, dict) else None
                    if isinstance(changed_thread_id, str):
                        tail_cache.pop(changed_thread_id, None)
                if message is not None and "method" not in message:
                    request_id = _json_id(message)
                    expected_thread_id = (
                        pending_summary_reads.pop(request_id, None)
                        if request_id is not None
                        else None
                    )
                    result = message.get("result")
                    thread = _thread_from_result(result)
                    found_thread_id = _thread_id(thread) if thread is not None else None
                    cache_thread_id = found_thread_id or expected_thread_id
                    if thread is not None and cache_thread_id is not None:
                        cached = copy.deepcopy(result)
                        cached_thread = _thread_from_result(cached)
                        assert cached_thread is not None
                        cached_thread.pop("turns", None)
                        summaries[cache_thread_id] = cached
                await send_client(raw)

        try:
            client_task = asyncio.create_task(client_to_upstream(), name="tui-to-app-server")
            upstream_task = asyncio.create_task(upstream_to_client(), name="app-server-to-tui")
            done, pending = await asyncio.wait(
                {client_task, upstream_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                    await task
            for task in done:
                task.result()
        except ConnectionClosed:
            pass
        finally:
            for task in intercepted:
                task.cancel()
            for task in intercepted:
                with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                    await task
            await upstream.close()

    async def _handle_full_read(
        self,
        *,
        request: JsonObject,
        raw_request: str | bytes,
        summaries: dict[str, JsonObject],
        tail_cache: dict[str, list[Any]],
        send_upstream: SendUpstream,
        send_client: SendClient,
    ) -> None:
        request_id = _json_id(request)
        params = request.get("params")
        if request_id is None or not isinstance(params, dict):
            await send_upstream(raw_request)
            return
        requested_thread_id = params.get("threadId")
        if not isinstance(requested_thread_id, str):
            await send_upstream(raw_request)
            return

        summary = summaries.get(requested_thread_id)
        helper: AppServerClient | None = None
        try:
            if summary is None:
                helper = await self._new_helper()
                result = await helper.call(
                    "thread/read", {"threadId": requested_thread_id, "includeTurns": False}
                )
                if not isinstance(result, dict) or _thread_from_result(result) is None:
                    raise AppServerError("thread/read returned no thread summary")
                summary = copy.deepcopy(result)
                summary_thread = _thread_from_result(summary)
                assert summary_thread is not None
                summary_thread.pop("turns", None)
                summaries[requested_thread_id] = summary

            thread = _thread_from_result(summary)
            assert thread is not None
            if _history_mode(thread) != "legacy":
                await send_upstream(raw_request)
                return
            size = _rollout_size(thread)
            should_protect = self.legacy_history_mode == "omit" or (
                self.legacy_history_mode == "auto"
                and (size is None or size >= self.history_threshold_bytes)
            )
            if not should_protect:
                await send_upstream(raw_request)
                return

            turns: list[Any] = []
            if self.legacy_history_mode == "auto":
                cached_turns = tail_cache.get(requested_thread_id)
                if cached_turns is not None:
                    turns = copy.deepcopy(cached_turns)
                else:
                    if helper is None:
                        helper = await self._new_helper()
                    try:
                        page = await helper.call(
                            "thread/turns/list",
                            {
                                "threadId": requested_thread_id,
                                "limit": self.history_tail_turns,
                                "sortDirection": "desc",
                                "itemsView": "full",
                            },
                        )
                        newest_first = page.get("data") if isinstance(page, dict) else None
                        if not isinstance(newest_first, list):
                            raise AppServerError("thread/turns/list returned invalid data")
                        turns = list(reversed(newest_first))
                    except Exception as exc:
                        print(
                            "codex-longrun history proxy: bounded tail unavailable for "
                            f"{requested_thread_id}; continuing without visible history: "
                            f"{type(exc).__name__}",
                            file=sys.stderr,
                        )
                    tail_cache[requested_thread_id] = copy.deepcopy(turns)

            response_result = copy.deepcopy(summary)
            response_thread = _thread_from_result(response_result)
            assert response_thread is not None
            response_thread["turns"] = turns
            response: JsonObject = {"id": request_id, "result": response_result}
            encoded = json.dumps(response, separators=(",", ":"), ensure_ascii=True)
            if len(encoded.encode()) > self.helper_max_message_bytes:
                response_thread["turns"] = []
                print(
                    "codex-longrun history proxy: bounded tail exceeded its message limit for "
                    f"{requested_thread_id}; continuing without visible history",
                    file=sys.stderr,
                )
            await send_client(response)
        except Exception as exc:
            if summary is not None:
                response_result = copy.deepcopy(summary)
                response_thread = _thread_from_result(response_result)
                if response_thread is not None:
                    response_thread["turns"] = []
                    await send_client({"id": request_id, "result": response_result})
                    print(
                        "codex-longrun history proxy: history protection fallback used for "
                        f"{requested_thread_id}: {type(exc).__name__}",
                        file=sys.stderr,
                    )
                    return
            await send_client(
                {
                    "id": request_id,
                    "error": {
                        "code": -32001,
                        "message": "Legacy history could not be read safely",
                    },
                }
            )
            print(
                "codex-longrun history proxy: cannot obtain a safe summary for "
                f"{requested_thread_id}: {type(exc).__name__}",
                file=sys.stderr,
            )
        finally:
            if helper is not None:
                await helper.close()

    async def _new_helper(self) -> AppServerClient:
        helper = AppServerClient(
            str(self.app_server_socket),
            request_timeout_sec=self.helper_timeout_sec,
            max_message_bytes=self.helper_max_message_bytes,
        )
        await helper.connect()
        return helper


async def _run(args: argparse.Namespace) -> None:
    proxy = TuiCompatibilityProxy(
        Path(args.app_server_socket),
        Path(args.tui_socket),
        legacy_history_mode=args.legacy_history_mode,
        history_threshold_bytes=args.history_threshold_mib << 20,
        history_tail_turns=args.history_tail_turns,
        helper_timeout_sec=args.helper_timeout_sec,
    )
    await proxy.start()
    try:
        await proxy.serve()
    finally:
        await proxy.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-server-socket", required=True)
    parser.add_argument("--tui-socket", required=True)
    parser.add_argument("--legacy-history-mode", choices=sorted(VALID_MODES), default="auto")
    parser.add_argument("--history-threshold-mib", type=int, default=64)
    parser.add_argument("--history-tail-turns", type=int, default=5)
    parser.add_argument("--helper-timeout-sec", type=float, default=120.0)
    args = parser.parse_args()
    if not 1 <= args.history_threshold_mib <= 127:
        parser.error("--history-threshold-mib must be between 1 and 127")
    if not 1 <= args.history_tail_turns <= 20:
        parser.error("--history-tail-turns must be between 1 and 20")
    if not 5 <= args.helper_timeout_sec <= 600:
        parser.error("--helper-timeout-sec must be between 5 and 600")
    return args


def main() -> None:
    try:
        asyncio.run(_run(_parse_args()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
