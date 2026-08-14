"""Helper process for the parent-death integration test."""

from __future__ import annotations

import asyncio
import os
import sys

from codex_mcp_longrun import server


CHILD_CODE = """
import os
import pathlib
import sys
import time

pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(60)
"""


async def main() -> None:
    result = await server.run_and_wait(
        argv=[sys.executable, "-c", CHILD_CODE, os.environ["TEST_CHILD_PID_FILE"]],
        cwd=os.environ["TEST_ROOT"],
        timeout_sec=60,
        grace_period_sec=1,
    )
    raise SystemExit(0 if result.state == "succeeded" else 1)


asyncio.run(main())
