"""Private Unix-socket protocol between the MCP server and Goal bridge."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import struct
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
DEFAULT_REQUEST_TIMEOUT_SEC = 5.0


class BridgeError(RuntimeError):
    """Raised when the local bridge rejects or cannot process a request."""


def _validate_private_socket(path: Path) -> None:
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise BridgeError(f"longrun bridge socket does not exist: {path}") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise BridgeError(f"longrun bridge path is not a Unix socket: {path}")
    if info.st_uid != os.getuid():
        raise BridgeError(f"longrun bridge socket is not owned by the current user: {path}")
    if info.st_mode & 0o077:
        raise BridgeError(f"longrun bridge socket permissions are too broad: {path}")


async def request_bridge(
    socket_path: str | Path,
    request: dict[str, Any],
    *,
    timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Send one bounded JSON-line request to the same-user local bridge."""
    path = Path(socket_path).expanduser().resolve()
    _validate_private_socket(path)
    payload = {"version": PROTOCOL_VERSION, **request}
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise BridgeError("longrun bridge request is too large")

    async def exchange() -> bytes:
        reader, writer = await asyncio.open_unix_connection(str(path))
        try:
            writer.write(encoded)
            await writer.drain()
            return await reader.readline()
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        response_line = await asyncio.wait_for(exchange(), timeout=timeout_sec)
    except TimeoutError as exc:
        raise BridgeError("longrun bridge request timed out") from exc
    except OSError as exc:
        raise BridgeError(f"longrun bridge connection failed: {exc}") from exc

    if not response_line:
        raise BridgeError("longrun bridge closed the connection without a response")
    if len(response_line) > MAX_MESSAGE_BYTES:
        raise BridgeError("longrun bridge response is too large")
    try:
        response = json.loads(response_line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("longrun bridge returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise BridgeError("longrun bridge returned a non-object response")
    if response.get("ok") is not True:
        message = response.get("error")
        raise BridgeError(str(message) if message else "longrun bridge rejected the request")
    return response


def peer_uid(writer: asyncio.StreamWriter) -> int | None:
    """Return the Linux SO_PEERCRED UID for a Unix-stream peer."""
    sock = writer.get_extra_info("socket")
    if sock is None or not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _, uid, _ = struct.unpack("3i", credentials)
    except OSError:
        return None
    return uid
