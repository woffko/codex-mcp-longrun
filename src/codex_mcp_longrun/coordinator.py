"""Own the Goal bridge and session-aware TUI proxy in one supervised process."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from pathlib import Path

from .session_bridge import SessionBridge
from .tui_proxy import TuiCompatibilityProxy


async def run(args: argparse.Namespace) -> None:
    bridge = SessionBridge(args.app_server_socket, args.bridge_socket, args.state_db)
    proxy = TuiCompatibilityProxy(
        args.app_server_socket, args.tui_socket, session_bridge=bridge,
        legacy_history_mode=args.legacy_history_mode,
        history_threshold_bytes=args.history_threshold_mib * (1 << 20),
        history_tail_turns=args.history_tail_turns, helper_timeout_sec=args.helper_timeout_sec,
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    tasks = []
    try:
        await bridge.start()
        await proxy.start()
        tasks = [asyncio.create_task(bridge.serve()), asyncio.create_task(proxy.serve()),
                 asyncio.create_task(stopped.wait())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await proxy.close()
        await bridge.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("app-server-socket", "bridge-socket", "state-db", "tui-socket"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--legacy-history-mode", choices=("auto", "omit", "off"), default="auto")
    parser.add_argument("--history-threshold-mib", type=int, default=64)
    parser.add_argument("--history-tail-turns", type=int, default=5)
    parser.add_argument("--helper-timeout-sec", type=float, default=120)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
