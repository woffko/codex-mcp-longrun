"""Spawn one guarded command and stay alive as its launcher parent."""

from __future__ import annotations

import os
import subprocess
import sys
import time


def main() -> None:
    command = sys.argv[1:]
    if not command:
        raise SystemExit("a child command is required")
    parent_fd, launcher_fd = os.pipe()
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "codex_mcp_longrun.parent_guard",
            "--parent-pid",
            str(os.getpid()),
            "--parent-fd",
            str(parent_fd),
            "--grace-seconds",
            "1",
            "--",
            *command,
        ],
        stdin=subprocess.DEVNULL,
        pass_fds=(parent_fd,),
        start_new_session=True,
    )
    os.close(parent_fd)
    print(child.pid, flush=True)
    while child.poll() is None:
        time.sleep(0.05)
    os.close(launcher_fd)
    raise SystemExit(child.returncode)


if __name__ == "__main__":
    main()
