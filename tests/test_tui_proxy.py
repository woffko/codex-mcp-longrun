from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from websockets.asyncio.client import ClientConnection, unix_connect
from websockets.asyncio.server import ServerConnection, unix_serve

from codex_mcp_longrun.app_server_client import AppServerError
from codex_mcp_longrun.tui_proxy import TuiCompatibilityProxy


class FakeHelper:
    def __init__(
        self,
        summary: dict[str, Any],
        turns: list[dict[str, Any]],
        *,
        fail_turns: bool = False,
    ) -> None:
        self.summary = summary
        self.turns = turns
        self.fail_turns = fail_turns
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "thread/read":
            return self.summary
        if method == "thread/turns/list":
            if self.fail_turns:
                raise AppServerError("synthetic helper failure")
            return {"data": list(self.turns), "nextCursor": "older"}
        raise AssertionError(f"unexpected helper method: {method}")

    async def close(self) -> None:
        self.closed = True


class TestProxy(TuiCompatibilityProxy):
    __test__ = False

    def __init__(self, *args: Any, helpers: list[FakeHelper], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helpers = helpers

    async def _new_helper(self) -> FakeHelper:  # type: ignore[override]
        if not self.helpers:
            raise AssertionError("no fake helper is available")
        return self.helpers.pop(0)


class FakeRelayAppServer:
    def __init__(self, thread: dict[str, Any]) -> None:
        self.thread = thread
        self.full_reads = 0
        self.resume_requests: list[dict[str, Any]] = []
        self.client_notifications: list[dict[str, Any]] = []
        self.client_responses: list[dict[str, Any]] = []

    async def handler(self, connection: ServerConnection) -> None:
        async for raw in connection:
            message = json.loads(raw)
            method = message.get("method")
            request_id = message.get("id")
            if method is None:
                self.client_responses.append(message)
                continue
            if request_id is None:
                self.client_notifications.append(message)
                continue
            if method == "initialize":
                await connection.send(
                    json.dumps({"id": request_id, "result": {"userAgent": "fake-app-server"}})
                )
            elif method == "thread/resume":
                self.resume_requests.append(dict(message["params"]))
                thread = dict(self.thread)
                thread["turns"] = []
                await connection.send(json.dumps({"id": request_id, "result": {"thread": thread}}))
            elif method == "thread/read":
                if message["params"].get("includeTurns") is True:
                    self.full_reads += 1
                    thread = dict(self.thread)
                    thread["turns"] = [{"id": "upstream-full"}]
                else:
                    thread = dict(self.thread)
                    thread.pop("turns", None)
                await connection.send(json.dumps({"id": request_id, "result": {"thread": thread}}))
            elif method == "relay/test":
                await connection.send(
                    json.dumps({"method": "relay/notice", "params": {"value": 7}})
                )
                await connection.send(
                    json.dumps(
                        {
                            "id": "server-request-1",
                            "method": "approval/request",
                            "params": {"reason": "test"},
                        }
                    )
                )
                await connection.send(json.dumps({"id": request_id, "result": {"ok": True}}))
            else:
                await connection.send(
                    json.dumps({"id": request_id, "result": {"method": method}})
                )


class TuiProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="codex-longrun-tui-proxy-test-")
        self.root = Path(self.tempdir.name)
        self.app_socket = self.root / "app.sock"
        self.tui_socket = self.root / "tui.sock"
        self.rollout = self.root / "rollout.jsonl"
        self.rollout.write_bytes(b"{}\n")
        self.thread_id = "01900000-0000-7000-8000-000000000001"
        self.thread = {
            "id": self.thread_id,
            "path": str(self.rollout),
            "historyMode": "legacy",
            "status": {"type": "notLoaded"},
        }
        self.fake_app = FakeRelayAppServer(self.thread)
        try:
            self.app_server = await unix_serve(
                self.fake_app.handler, str(self.app_socket), compression=None
            )
        except PermissionError as exc:
            self.skipTest(f"sandbox does not permit Unix sockets: {exc}")
        self.proxy: TuiCompatibilityProxy | None = None
        self.client: ClientConnection | None = None

    async def asyncTearDown(self) -> None:
        if self.client is not None:
            await self.client.close()
        if self.proxy is not None:
            await self.proxy.close()
        self.app_server.close()
        await self.app_server.wait_closed()
        self.tempdir.cleanup()

    async def _start(
        self,
        *,
        helpers: list[FakeHelper] | None = None,
        mode: str = "auto",
        threshold: int = 1024,
    ) -> ClientConnection:
        self.proxy = TestProxy(
            self.app_socket,
            self.tui_socket,
            helpers=helpers or [],
            legacy_history_mode=mode,
            history_threshold_bytes=threshold,
            history_tail_turns=2,
            helper_timeout_sec=2,
            helper_max_message_bytes=64 << 10,
            proxy_max_message_bytes=1 << 20,
        )
        await self.proxy.start()
        self.assertEqual(self.tui_socket.stat().st_mode & 0o777, 0o600)
        self.client = await unix_connect(
            str(self.tui_socket),
            uri="ws://localhost/rpc",
            compression=None,
            max_size=1 << 20,
        )
        return self.client

    async def _request(
        self, client: ClientConnection, request_id: int, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        await client.send(json.dumps({"id": request_id, "method": method, "params": params}))
        while True:
            message = json.loads(await asyncio.wait_for(client.recv(), timeout=2))
            if message.get("id") == request_id:
                return message

    async def test_large_legacy_read_uses_bounded_tail_and_original_request_id(self) -> None:
        self.rollout.write_bytes(b"x" * 2048)
        helper = FakeHelper(
            {"thread": dict(self.thread)},
            [{"id": "newest"}, {"id": "older"}],
        )
        client = await self._start(helpers=[helper])

        summary = await self._request(
            client,
            1,
            "thread/read",
            {"threadId": self.thread_id, "includeTurns": False},
        )
        self.assertNotIn("turns", summary["result"]["thread"])
        full = await self._request(
            client,
            77,
            "thread/read",
            {"threadId": self.thread_id, "includeTurns": True},
        )

        self.assertEqual(full["id"], 77)
        self.assertEqual(
            [turn["id"] for turn in full["result"]["thread"]["turns"]],
            ["older", "newest"],
        )
        self.assertEqual(self.fake_app.full_reads, 0)
        self.assertEqual(helper.calls[0][0], "thread/turns/list")
        self.assertEqual(helper.calls[0][1]["itemsView"], "full")
        self.assertTrue(helper.closed)

        repeated = await self._request(
            client,
            78,
            "thread/read",
            {"threadId": self.thread_id, "includeTurns": True},
        )
        self.assertEqual(repeated["result"]["thread"]["turns"], full["result"]["thread"]["turns"])
        self.assertEqual(len(helper.calls), 1)

    async def test_tail_failure_falls_back_to_empty_visible_history(self) -> None:
        self.rollout.write_bytes(b"x" * 2048)
        helper = FakeHelper({"thread": dict(self.thread)}, [], fail_turns=True)
        client = await self._start(helpers=[helper])
        await self._request(
            client,
            1,
            "thread/read",
            {"threadId": self.thread_id, "includeTurns": False},
        )

        full = await self._request(
            client,
            2,
            "thread/read",
            {"threadId": self.thread_id, "includeTurns": True},
        )

        self.assertEqual(full["result"]["thread"]["turns"], [])
        self.assertEqual(self.fake_app.full_reads, 0)

    async def test_official_exclude_turns_resume_seeds_safe_history_read(self) -> None:
        self.rollout.write_bytes(b"x" * 2048)
        helper = FakeHelper({"thread": dict(self.thread)}, [{"id": "latest"}])
        client = await self._start(helpers=[helper])

        resumed = await self._request(
            client,
            1,
            "thread/resume",
            {"threadId": self.thread_id, "excludeTurns": True},
        )
        self.assertEqual(resumed["result"]["thread"]["turns"], [])
        full = await self._request(
            client,
            2,
            "thread/read",
            {"threadId": self.thread_id, "includeTurns": True},
        )

        self.assertEqual(full["result"]["thread"]["turns"], [{"id": "latest"}])
        self.assertEqual(
            self.fake_app.resume_requests,
            [{"threadId": self.thread_id, "excludeTurns": True}],
        )
        self.assertEqual([method for method, _ in helper.calls], ["thread/turns/list"])
        self.assertEqual(self.fake_app.full_reads, 0)

    async def test_uncached_large_read_fetches_summary_before_tail(self) -> None:
        self.rollout.write_bytes(b"x" * 2048)
        helper = FakeHelper(
            {"thread": dict(self.thread)},
            [{"id": "latest"}],
        )
        client = await self._start(helpers=[helper])

        full = await self._request(
            client,
            9,
            "thread/read",
            {"threadId": self.thread_id, "includeTurns": True},
        )

        self.assertEqual(full["result"]["thread"]["turns"], [{"id": "latest"}])
        self.assertEqual(
            [method for method, _ in helper.calls],
            ["thread/read", "thread/turns/list"],
        )
        self.assertEqual(self.fake_app.full_reads, 0)

    async def test_small_legacy_read_is_transparent(self) -> None:
        client = await self._start()
        await self._request(
            client,
            1,
            "thread/read",
            {"threadId": self.thread_id, "includeTurns": False},
        )

        full = await self._request(
            client,
            2,
            "thread/read",
            {"threadId": self.thread_id, "includeTurns": True},
        )

        self.assertEqual(full["result"]["thread"]["turns"], [{"id": "upstream-full"}])
        self.assertEqual(self.fake_app.full_reads, 1)

    async def test_omit_mode_never_calls_tail_helper(self) -> None:
        client = await self._start(mode="omit")
        await self._request(
            client,
            1,
            "thread/read",
            {"threadId": self.thread_id, "includeTurns": False},
        )
        full = await self._request(
            client,
            2,
            "thread/read",
            {"threadId": self.thread_id, "includeTurns": True},
        )
        self.assertEqual(full["result"]["thread"]["turns"], [])
        self.assertEqual(self.fake_app.full_reads, 0)

    async def test_notifications_server_requests_and_responses_are_transparent(self) -> None:
        client = await self._start(mode="off")
        await client.send(json.dumps({"method": "client/notice", "params": {"value": 3}}))
        await client.send(json.dumps({"id": 5, "method": "relay/test", "params": {}}))

        received: list[dict[str, Any]] = []
        while len(received) < 3:
            received.append(json.loads(await asyncio.wait_for(client.recv(), timeout=2)))
        methods = {message.get("method") for message in received}
        self.assertIn("relay/notice", methods)
        server_request = next(
            message for message in received if message.get("id") == "server-request-1"
        )
        self.assertEqual(server_request["method"], "approval/request")
        self.assertTrue(any(message.get("id") == 5 for message in received))

        await client.send(json.dumps({"id": "server-request-1", "result": {"approved": True}}))
        for _ in range(100):
            if self.fake_app.client_responses:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(
            self.fake_app.client_responses,
            [{"id": "server-request-1", "result": {"approved": True}}],
        )
        self.assertEqual(
            self.fake_app.client_notifications,
            [{"method": "client/notice", "params": {"value": 3}}],
        )


if __name__ == "__main__":
    unittest.main()
