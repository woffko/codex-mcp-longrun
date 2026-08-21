"""Supervise a launcher child and tear down its process group on parent loss."""

from __future__ import annotations

import argparse
import ctypes
import os
import select
import signal
import sys
import time
from collections.abc import Sequence


PR_SET_PDEATHSIG = 1
DEATH_SIGNAL = signal.SIGTERM
_termination_requested = False


def _request_termination(_signum: int, _frame: object) -> None:
    global _termination_requested
    _termination_requested = True


def _parse_args(
    argv: Sequence[str] | None = None,
) -> tuple[int, int | None, int, bool, list[str]]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--parent-fd", type=int)
    parser.add_argument("--grace-seconds", type=int, default=2)
    parser.add_argument("--exec-only", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    command = list(arguments.command)
    if command and command[0] == "--":
        command.pop(0)
    if arguments.parent_pid <= 1:
        parser.error("--parent-pid must identify a live launcher process")
    if not 1 <= arguments.grace_seconds <= 30:
        parser.error("--grace-seconds must be between 1 and 30")
    if arguments.exec_only and arguments.parent_fd is not None:
        parser.error("--exec-only cannot be combined with --parent-fd")
    if not arguments.exec_only and (
        arguments.parent_fd is None or arguments.parent_fd < 3
    ):
        parser.error("supervisor mode requires --parent-fd outside standard streams")
    if not command:
        parser.error("a command is required after --")
    return (
        arguments.parent_pid,
        arguments.parent_fd,
        arguments.grace_seconds,
        arguments.exec_only,
        command,
    )


def _unblock_death_signal() -> None:
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {DEATH_SIGNAL})


def _arm_parent_death_signal(parent_pid: int) -> None:
    if sys.platform != "linux":
        raise RuntimeError("parent-death supervision requires Linux or WSL")

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
    if prctl(PR_SET_PDEATHSIG, int(DEATH_SIGNAL), 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))

    # PR_SET_PDEATHSIG does not report a parent that exited before prctl().
    # Re-reading PPID after arming closes that fork-to-arm race.
    if os.getppid() != parent_pid:
        raise SystemExit(128 + int(DEATH_SIGNAL))


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


def _exec_only(parent_pid: int, command: list[str]) -> None:
    signal.signal(DEATH_SIGNAL, signal.SIG_DFL)
    _unblock_death_signal()
    _arm_parent_death_signal(parent_pid)
    os.execvpe(command[0], command, os.environ)


def _supervise(parent_fd: int, command: list[str], grace_period_sec: int) -> int:
    guard_pid = os.getpid()
    if _termination_requested:
        return 128 + int(DEATH_SIGNAL)

    command_pid = os.fork()
    if command_pid == 0:
        os.close(parent_fd)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        _unblock_death_signal()
        _arm_parent_death_signal(guard_pid)
        os.setsid()
        try:
            os.execvpe(command[0], command, os.environ)
        except OSError as exc:
            print(f"exec error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            os._exit(127)

    termination_started_at: float | None = None
    try:
        while True:
            wait_info = os.waitid(
                os.P_PID, command_pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
            )
            if wait_info is not None:
                # Sweep intentionally detached descendants before reaping the
                # group leader and releasing its PID for reuse.
                _signal_command_group(command_pid, signal.SIGTERM)
                time.sleep(0.1)
                _signal_command_group(command_pid, signal.SIGKILL)
                _waited_pid, wait_status = os.waitpid(command_pid, 0)
                return _exit_code(wait_status)

            if not _termination_requested:
                ready, _writable, _exceptional = select.select(
                    [parent_fd], [], [], 0.05
                )
                if ready and os.read(parent_fd, 1) == b"":
                    _request_termination(signal.SIGTERM, None)

            if _termination_requested:
                if termination_started_at is None:
                    termination_started_at = time.monotonic()
                elapsed = time.monotonic() - termination_started_at
                signal_to_send = (
                    signal.SIGKILL if elapsed >= grace_period_sec else signal.SIGTERM
                )
                # Retry until the forked child has completed setsid() and owns
                # the process group identified by command_pid.
                _signal_command_group(command_pid, signal_to_send)
                time.sleep(0.05)
    finally:
        os.close(parent_fd)


def main() -> None:
    parent_pid, parent_fd, grace_period_sec, exec_only, command = _parse_args()
    try:
        if exec_only:
            _exec_only(parent_pid, command)
            raise AssertionError("exec returned unexpectedly")

        assert parent_fd is not None
        signal.signal(signal.SIGTERM, _request_termination)
        signal.signal(signal.SIGINT, _request_termination)
        signal.signal(signal.SIGHUP, _request_termination)
        _unblock_death_signal()
        _arm_parent_death_signal(parent_pid)
        raise SystemExit(_supervise(parent_fd, command, grace_period_sec))
    except OSError as exc:
        raise SystemExit(f"codex-longrun parent guard: {exc}") from exc
    except RuntimeError as exc:
        raise SystemExit(f"codex-longrun parent guard: {exc}") from exc


if __name__ == "__main__":
    main()
