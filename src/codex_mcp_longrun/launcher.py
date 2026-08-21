"""Launch the official Codex App Server, Goal bridge, and remote TUI together."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


STARTUP_TIMEOUT_SEC = 20.0


@dataclass(frozen=True)
class LauncherOptions:
    legacy_history_mode: str = "auto"
    history_threshold_mib: int = 64
    history_tail_turns: int = 5
    helper_timeout_sec: float = 120.0


def _runtime_parent() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    candidate = Path(configured).expanduser() if configured else Path("/tmp")
    candidate = candidate.resolve()
    if not candidate.is_dir():
        raise RuntimeError(f"runtime parent does not exist: {candidate}")
    info = candidate.stat()
    if configured and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise RuntimeError(f"XDG_RUNTIME_DIR is not private and same-user owned: {candidate}")
    return candidate


def _make_runtime_dir() -> Path:
    preferred = _runtime_parent()
    candidates = [preferred]
    fallback = Path("/tmp").resolve()
    if fallback != preferred:
        candidates.append(fallback)
    last_error: OSError | None = None
    for parent in candidates:
        try:
            path = Path(tempfile.mkdtemp(prefix="codex-longrun-", dir=parent))
        except OSError as exc:
            last_error = exc
            continue
        path.chmod(0o700)
        return path
    raise RuntimeError(f"cannot create a private runtime directory: {last_error}")


async def _wait_for_socket(path: Path, process: asyncio.subprocess.Process, label: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if path.is_socket():
            return
        if process.returncode is not None:
            raise RuntimeError(f"{label} exited during startup with code {process.returncode}")
        await asyncio.sleep(0.05)
    raise RuntimeError(f"{label} did not create {path} within {STARTUP_TIMEOUT_SEC:g} seconds")


async def _terminate(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


def _guarded_exec_command(*command: str) -> tuple[str, ...]:
    """Exec an interactive child with kernel parent-death delivery."""

    return (
        sys.executable,
        "-m",
        "codex_mcp_longrun.parent_guard",
        "--parent-pid",
        str(os.getpid()),
        "--exec-only",
        "--",
        *command,
    )


async def _spawn_supervised_daemon(
    *command: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[asyncio.subprocess.Process, int]:
    """Start a daemon guard isolated from the launcher's terminal group."""

    parent_fd, launcher_fd = os.pipe()
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "codex_mcp_longrun.parent_guard",
            "--parent-pid",
            str(os.getpid()),
            "--parent-fd",
            str(parent_fd),
            "--",
            *command,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            pass_fds=(parent_fd,),
            start_new_session=True,
        )
    except BaseException:
        os.close(launcher_fd)
        raise
    finally:
        os.close(parent_fd)
    return process, launcher_fd


def _resolve_codex_cwd(
    codex_args: list[str], invocation_cwd: Path | None = None
) -> Path:
    """Resolve Codex's -C/--cd before starting the App Server."""

    base = (invocation_cwd or Path.cwd()).expanduser().resolve()
    requested: str | None = None
    index = 0
    while index < len(codex_args):
        argument = codex_args[index]
        if argument == "--":
            break
        if argument in {"-C", "--cd"}:
            if index + 1 >= len(codex_args):
                raise RuntimeError(f"{argument} requires a working-directory argument")
            requested = codex_args[index + 1]
            index += 2
            continue
        if argument.startswith("--cd="):
            requested = argument.removeprefix("--cd=")
        index += 1

    candidate = Path(requested).expanduser() if requested is not None else base
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"Codex working directory does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise RuntimeError(f"Codex working directory is not a directory: {resolved}")
    return resolved


async def _run(codex_args: list[str], options: LauncherOptions) -> int:
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("codex executable was not found in PATH")
    codex_cwd = _resolve_codex_cwd(codex_args)
    runtime_dir = _make_runtime_dir()
    app_socket = runtime_dir / "app.sock"
    bridge_socket = runtime_dir / "bridge.sock"
    tui_socket = runtime_dir / "tui.sock"
    state_db = runtime_dir / "bridge.sqlite3"
    sockets = (app_socket, bridge_socket, tui_socket)
    if any(len(str(path).encode()) >= 100 for path in sockets):
        shutil.rmtree(runtime_dir, ignore_errors=True)
        raise RuntimeError("runtime path is too long for Unix-domain sockets")

    app_process: asyncio.subprocess.Process | None = None
    bridge_process: asyncio.subprocess.Process | None = None
    proxy_process: asyncio.subprocess.Process | None = None
    tui_process: asyncio.subprocess.Process | None = None
    guard_launcher_fds: list[int] = []
    try:
        app_env = dict(os.environ)
        app_env["LONGRUN_BRIDGE_SOCKET"] = str(bridge_socket)
        app_process, app_guard_fd = await _spawn_supervised_daemon(
            codex,
            "app-server",
            "--listen",
            f"unix://{app_socket}",
            cwd=str(codex_cwd),
            env=app_env,
        )
        guard_launcher_fds.append(app_guard_fd)
        await _wait_for_socket(app_socket, app_process, "Codex App Server")

        bridge_process, bridge_guard_fd = await _spawn_supervised_daemon(
            sys.executable,
            "-m",
            "codex_mcp_longrun.bridge",
            "--app-server-socket",
            str(app_socket),
            "--bridge-socket",
            str(bridge_socket),
            "--state-db",
            str(state_db),
        )
        guard_launcher_fds.append(bridge_guard_fd)
        await _wait_for_socket(bridge_socket, bridge_process, "codex-longrun bridge")

        proxy_process, proxy_guard_fd = await _spawn_supervised_daemon(
            sys.executable,
            "-m",
            "codex_mcp_longrun.tui_proxy",
            "--app-server-socket",
            str(app_socket),
            "--tui-socket",
            str(tui_socket),
            "--legacy-history-mode",
            options.legacy_history_mode,
            "--history-threshold-mib",
            str(options.history_threshold_mib),
            "--history-tail-turns",
            str(options.history_tail_turns),
            "--helper-timeout-sec",
            str(options.helper_timeout_sec),
        )
        guard_launcher_fds.append(proxy_guard_fd)
        await _wait_for_socket(tui_socket, proxy_process, "codex-longrun TUI proxy")

        tui_process = await asyncio.create_subprocess_exec(
            *_guarded_exec_command(
                codex,
                "--remote",
                f"unix://{tui_socket}",
                *codex_args,
            ),
            cwd=str(codex_cwd),
        )
        tui_wait = asyncio.create_task(tui_process.wait())
        proxy_wait = asyncio.create_task(proxy_process.wait())
        bridge_wait = asyncio.create_task(bridge_process.wait())
        app_wait = asyncio.create_task(app_process.wait())
        waits = {tui_wait, proxy_wait, bridge_wait, app_wait}
        done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if tui_wait in done:
            return tui_wait.result()
        if proxy_wait in done:
            raise RuntimeError(
                f"TUI compatibility proxy exited unexpectedly with code {proxy_wait.result()}"
            )
        if bridge_wait in done:
            raise RuntimeError(f"Goal bridge exited unexpectedly with code {bridge_wait.result()}")
        raise RuntimeError(f"Codex App Server exited unexpectedly with code {app_wait.result()}")
    finally:
        await _terminate(tui_process)
        await _terminate(proxy_process)
        await _terminate(bridge_process)
        await _terminate(app_process)
        for guard_fd in guard_launcher_fds:
            with contextlib.suppress(OSError):
                os.close(guard_fd)
        # The target is the exact private directory returned by mkdtemp.
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _parse_args() -> tuple[list[str], LauncherOptions]:
    parser = argparse.ArgumentParser(
        prog="codex-longrun",
        description="Run the official Codex TUI with event-driven longrun Goal wakeups.",
        add_help=False,
    )
    parser.add_argument("--bridge-help", action="store_true")
    parser.add_argument(
        "--longrun-legacy-history",
        choices=("auto", "omit", "off"),
        default="auto",
    )
    parser.add_argument("--longrun-history-threshold-mib", type=int, default=64)
    parser.add_argument("--longrun-history-tail-turns", type=int, default=5)
    parser.add_argument("--longrun-history-timeout-sec", type=float, default=120.0)
    known, remaining = parser.parse_known_args()
    if known.bridge_help:
        print(
            "Usage: codex-longrun [CODEX_OPTIONS] [PROMPT]\n"
            "       codex-longrun resume [CODEX_RESUME_OPTIONS] [SESSION_ID]\n\n"
            "Launcher options:\n"
            "  --longrun-legacy-history {auto,omit,off}\n"
            "  --longrun-history-threshold-mib N\n"
            "  --longrun-history-tail-turns N\n"
            "  --longrun-history-timeout-sec N\n\n"
            "Other arguments are passed to the official Codex CLI."
        )
        raise SystemExit(0)
    if not 1 <= known.longrun_history_threshold_mib <= 127:
        parser.error("--longrun-history-threshold-mib must be between 1 and 127")
    if not 1 <= known.longrun_history_tail_turns <= 20:
        parser.error("--longrun-history-tail-turns must be between 1 and 20")
    if not 5 <= known.longrun_history_timeout_sec <= 600:
        parser.error("--longrun-history-timeout-sec must be between 5 and 600")
    options = LauncherOptions(
        legacy_history_mode=known.longrun_legacy_history,
        history_threshold_mib=known.longrun_history_threshold_mib,
        history_tail_turns=known.longrun_history_tail_turns,
        helper_timeout_sec=known.longrun_history_timeout_sec,
    )
    return remaining, options


def main() -> None:
    codex_args, options = _parse_args()
    try:
        raise SystemExit(asyncio.run(_run(codex_args, options)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except RuntimeError as exc:
        raise SystemExit(f"codex-longrun: {exc}") from exc


if __name__ == "__main__":
    main()
