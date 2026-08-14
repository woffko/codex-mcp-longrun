"""Linux process supervisor with a parent-death guarantee."""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import time


PR_SET_PDEATHSIG = 1
_termination_requested = False


def _request_termination(_signum: int, _frame: object) -> None:
    global _termination_requested
    _termination_requested = True


def _enable_parent_death_signal(expected_parent_pid: int) -> None:
    if os.name != "posix" or not sys.platform.startswith("linux"):
        raise RuntimeError("codex-mcp-longrun currently supports Linux and WSL only")

    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    if prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))

    # Close the race in which the parent exits before PR_SET_PDEATHSIG is set.
    if os.getppid() != expected_parent_pid:
        raise SystemExit(143)


def _signal_command_group(command_pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(command_pid, sig)
    except ProcessLookupError:
        pass


def _exit_code(wait_status: int) -> int:
    if os.WIFEXITED(wait_status):
        return os.WEXITSTATUS(wait_status)
    if os.WIFSIGNALED(wait_status):
        return 128 + os.WTERMSIG(wait_status)
    return 1


def _supervise(command: list[str], grace_period_sec: int, status_fd: int) -> int:
    command_pid = os.fork()
    if command_pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.setsid()
        os.write(status_fd, f"{os.getpid()}\n".encode("ascii"))
        os.close(status_fd)
        try:
            os.execvpe(command[0], command, os.environ)
        except OSError as exc:
            print(f"exec error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            os._exit(127)

    os.close(status_fd)
    termination_started_at: float | None = None
    while True:
        wait_info = os.waitid(os.P_PID, command_pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if wait_info is not None:
            # Do not allow intentionally detached descendants to outlive the job.
            _signal_command_group(command_pid, signal.SIGTERM)
            time.sleep(0.1)
            _signal_command_group(command_pid, signal.SIGKILL)
            _waited_pid, wait_status = os.waitpid(command_pid, 0)
            return _exit_code(wait_status)

        if _termination_requested:
            if termination_started_at is None:
                termination_started_at = time.monotonic()
            if time.monotonic() - termination_started_at >= grace_period_sec:
                _signal_command_group(command_pid, signal.SIGKILL)
            else:
                # Retry until the child has completed setsid() and owns its group.
                _signal_command_group(command_pid, signal.SIGTERM)
        time.sleep(0.05)


def main() -> None:
    if len(sys.argv) < 5:
        raise SystemExit(
            "usage: python -m codex_mcp_longrun.job_exec "
            "PARENT_PID GRACE_SECONDS STATUS_FD COMMAND [ARG ...]"
        )

    try:
        expected_parent_pid = int(sys.argv[1])
    except ValueError as exc:
        raise SystemExit("parent PID must be an integer") from exc

    try:
        grace_period_sec = int(sys.argv[2])
    except ValueError as exc:
        raise SystemExit("grace period must be an integer") from exc
    if not 1 <= grace_period_sec <= 120:
        raise SystemExit("grace period must be between 1 and 120 seconds")

    try:
        status_fd = int(sys.argv[3])
    except ValueError as exc:
        raise SystemExit("status fd must be an integer") from exc
    if status_fd < 3:
        raise SystemExit("status fd must not be a standard stream")

    command = sys.argv[4:]
    os.umask(0o077)
    signal.signal(signal.SIGTERM, _request_termination)
    signal.signal(signal.SIGINT, _request_termination)
    _enable_parent_death_signal(expected_parent_pid)
    raise SystemExit(_supervise(command, grace_period_sec, status_fd))


if __name__ == "__main__":
    main()
