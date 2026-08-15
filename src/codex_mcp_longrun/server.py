"""Local STDIO MCP server for bounded long-running commands."""

from __future__ import annotations

import asyncio
import codecs
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Annotated, Literal

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.shared.message import SessionMessage
from mcp import types as mcp_types
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .bridge_protocol import BridgeError, request_bridge


SERVER_NAME = "Codex MCP Longrun"
SERVER_VERSION = "0.4.0a2"
DEFAULT_MAX_LOG_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_TIMEOUT_SEC = 12 * 60 * 60
DEFAULT_HEARTBEAT_INITIAL_SEC = 0
DEFAULT_HEARTBEAT_INTERVAL_SEC = 15 * 60
DEFAULT_MAX_ACTIVE_JOBS = 4
MAX_MEMORY_TAIL_CHARS = 64 * 1024
MAX_MATCH_TEXT_LENGTH = 4096
JOB_ID_RE = re.compile(r"^(?:[a-f0-9]{12}|[a-f0-9]{32})$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_ENV_PARTS = (
    "api_key",
    "apikey",
    "auth",
    "bearer",
    "credential",
    "cookie",
    "password",
    "passwd",
    "private_key",
    "secret",
    "session",
    "token",
)
DEFAULT_FORWARD_ENV_NAMES = (
    "HOME",
    "USER",
    "LOGNAME",
    "PATH",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TZ",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
SHELL_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "dash",
    "fish",
    "powershell",
    "pwsh",
    "sh",
    "zsh",
}
ELEVATION_EXECUTABLES = {"doas", "pkexec", "su", "sudo"}


def _read_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _read_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value, got {raw!r}")


def _load_allowed_roots() -> list[Path]:
    raw = os.environ.get("LONGRUN_ALLOWED_ROOTS", "")
    if not raw:
        raise RuntimeError("LONGRUN_ALLOWED_ROOTS must contain at least one absolute trusted root")
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        root = Path(part).expanduser()
        if not root.is_absolute():
            raise RuntimeError(f"LONGRUN_ALLOWED_ROOTS contains a relative path: {part!r}")
        roots.append(root.resolve(strict=True))
    if not roots:
        raise RuntimeError("LONGRUN_ALLOWED_ROOTS resolved to an empty list")
    return roots


def _is_sensitive_env_name(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in SECRET_ENV_PARTS)


def _load_forward_env_names() -> tuple[str, ...]:
    raw = os.environ.get("LONGRUN_FORWARD_ENV_NAMES")
    candidates = DEFAULT_FORWARD_ENV_NAMES if not raw else tuple(part.strip() for part in raw.split(","))
    names: list[str] = []
    for name in candidates:
        if not name:
            continue
        if not ENV_NAME_RE.fullmatch(name):
            raise RuntimeError(f"LONGRUN_FORWARD_ENV_NAMES contains an invalid name: {name!r}")
        if _is_sensitive_env_name(name):
            raise RuntimeError(f"LONGRUN_FORWARD_ENV_NAMES contains a sensitive name: {name!r}")
        if name not in names:
            names.append(name)
    return tuple(names)


STATE_DIR = Path(
    os.environ.get("LONGRUN_STATE_DIR", str(Path.home() / ".local" / "state" / "codex-longrun"))
).expanduser().resolve()
JOBS_DIR = STATE_DIR / "jobs"
JOBS_LOCK_PATH = STATE_DIR / ".jobs.lock"
MAX_LOG_BYTES = _read_int_env(
    "LONGRUN_MAX_LOG_BYTES",
    DEFAULT_MAX_LOG_BYTES,
    1 * 1024 * 1024,
    2 * 1024 * 1024 * 1024,
)
MAX_TIMEOUT_SEC = _read_int_env(
    "LONGRUN_MAX_TIMEOUT_SEC",
    DEFAULT_MAX_TIMEOUT_SEC,
    60,
    7 * 24 * 60 * 60,
)
HEARTBEAT_INITIAL_SEC = _read_int_env(
    "LONGRUN_HEARTBEAT_INITIAL_SEC",
    DEFAULT_HEARTBEAT_INITIAL_SEC,
    0,
    24 * 60 * 60,
)
HEARTBEAT_INTERVAL_SEC = _read_int_env(
    "LONGRUN_HEARTBEAT_INTERVAL_SEC",
    DEFAULT_HEARTBEAT_INTERVAL_SEC,
    1,
    24 * 60 * 60,
)
MAX_ACTIVE_JOBS = _read_int_env(
    "LONGRUN_MAX_ACTIVE_JOBS",
    DEFAULT_MAX_ACTIVE_JOBS,
    1,
    64,
)
ALLOWED_ROOTS = _load_allowed_roots()
FORWARD_ENV_NAMES = _load_forward_env_names()
ALLOW_SHELL = _read_bool_env("LONGRUN_ALLOW_SHELL", False)
DEBUG_STDIO = _read_bool_env("LONGRUN_DEBUG_STDIO", False)
BRIDGE_SOCKET = os.environ.get("LONGRUN_BRIDGE_SOCKET", "").strip()


def _debug_stdio(message: str) -> None:
    if DEBUG_STDIO:
        print(f"codex-longrun stdio: {message}", file=sys.stderr, flush=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _ensure_state_dirs() -> None:
    _ensure_private_dir(STATE_DIR)
    _ensure_private_dir(JOBS_DIR)


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(mode)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, value: dict[str, object]) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def _locked_job_state():
    _ensure_state_dirs()
    with JOBS_LOCK_PATH.open("a+b") as lock_file:
        JOBS_LOCK_PATH.chmod(0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _proc_start_ticks(pid: int) -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat_text[stat_text.rfind(")") + 2 :].split()
        return fields[19]
    except (FileNotFoundError, IndexError, OSError):
        return None


def _pid_matches(pid: object, start_ticks: object) -> bool:
    if not isinstance(pid, int) or not isinstance(start_ticks, str):
        return False
    return _proc_start_ticks(pid) == start_ticks


SERVER_START_TICKS = _proc_start_ticks(os.getpid())


def _recover_orphaned_jobs_locked() -> int:
    _ensure_state_dirs()
    recovered = 0
    for metadata_path in JOBS_DIR.glob("*.json"):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("state") not in {"queued", "starting", "running"}:
            continue
        if _pid_matches(payload.get("server_pid"), payload.get("server_start_ticks")):
            continue

        command_pgid = payload.get("command_pgid")
        command_start_ticks = payload.get("command_start_ticks")
        command_group_terminated = False
        if _pid_matches(command_pgid, command_start_ticks) and isinstance(command_pgid, int):
            try:
                if os.getpgid(command_pgid) == command_pgid:
                    os.killpg(command_pgid, signal.SIGKILL)
                    command_group_terminated = True
            except (ProcessLookupError, PermissionError):
                pass

        supervisor_pid = payload.get("pid")
        supervisor_start_ticks = payload.get("process_start_ticks")
        supervisor_terminated = False
        if _pid_matches(supervisor_pid, supervisor_start_ticks) and isinstance(supervisor_pid, int):
            try:
                os.kill(supervisor_pid, signal.SIGKILL)
                supervisor_terminated = True
            except (ProcessLookupError, PermissionError):
                pass

        payload.update(
            {
                "state": "orphaned_recovered",
                "finished_at_utc": _utc_now(),
                "recovery_terminated_command_group": command_group_terminated,
                "recovery_terminated_supervisor": supervisor_terminated,
                "error": "the owning MCP server exited before recording a terminal result",
            }
        )
        _atomic_write_json(metadata_path, payload)
        recovered += 1
    return recovered


def _recover_orphaned_jobs() -> int:
    with _locked_job_state():
        return _recover_orphaned_jobs_locked()


RECOVERED_ORPHAN_JOBS = _recover_orphaned_jobs()


MCP_INSTRUCTIONS = (
    "For a reviewed, trusted, non-interactive command expected to run longer than about 30 seconds, "
    "use start_job once. It returns promptly with a job_id while the local server supervises the "
    "command. Do not poll get_job in the same turn. When automatic_wakeup is true, the local bridge "
    "has paused the current durable Goal and will reactivate that same Goal after the terminal job "
    "metadata is committed and the current turn becomes idle. When automatic_wakeup is false, use "
    "get_job later for one bounded status read and do not claim automatic wakeup. cancel_job requires "
    "approval. run_and_wait is a legacy "
    "compatibility tool because some Codex runtimes turn a pending tool call into model-driven waits. "
    "Pass argv as an array and cwd as an absolute path. Never pass secrets. Shell and privilege-elevation "
    "commands are disabled. Full output stays in a private local log and only a bounded tail is returned. "
    "This server uses host-user permissions and is not a security sandbox."
)

mcp = MCPServer(
    "codex-longrun",
    title=SERVER_NAME,
    description="Runs bounded non-interactive commands with a prompt asynchronous job mode.",
    instructions=MCP_INSTRUCTIONS,
    version=SERVER_VERSION,
    log_level="WARNING",
)


class HealthResult(BaseModel):
    ok: bool
    server_version: str
    mcp_version: str
    python_version: str
    state_dir: str
    allowed_roots: list[str]
    forwarded_env_names: list[str]
    allow_shell: bool
    max_log_bytes: int
    max_timeout_sec: int
    max_active_jobs: int
    heartbeat_initial_sec: int
    heartbeat_interval_sec: int
    recovered_orphan_jobs: int
    bridge_configured: bool


RunState = Literal[
    "succeeded",
    "failed",
    "timed_out",
    "inactive_timeout",
    "success_condition_not_met",
    "spawn_error",
    "cancelled",
]

JobState = Literal[
    "queued",
    "starting",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "inactive_timeout",
    "success_condition_not_met",
    "spawn_error",
    "cancelled",
    "orphaned_recovered",
]


class RunResult(BaseModel):
    job_id: str
    state: RunState
    exit_code: int | None
    duration_sec: float
    started_at_utc: str
    finished_at_utc: str
    command_display: str
    cwd: str
    log_path: str
    metadata_path: str
    output_bytes_seen: int
    output_bytes_logged: int
    log_truncated: bool
    success_match: str | None = None
    failure_match: str | None = None
    error: str | None = None
    tail: str = ""


class LogTailResult(BaseModel):
    job_id: str
    source_path: str
    tail: str


class JobStatusResult(BaseModel):
    job_id: str
    state: JobState
    terminal: bool
    automatic_wakeup: bool = False
    wake_delivery: str | None = None
    recommended_check_after_sec: int | None = None
    exit_code: int | None = None
    duration_sec: float
    started_at_utc: str
    finished_at_utc: str | None = None
    command_display: str
    cwd: str
    log_path: str
    metadata_path: str
    output_bytes_seen: int = 0
    output_bytes_logged: int = 0
    log_truncated: bool = False
    success_match: str | None = None
    failure_match: str | None = None
    error: str | None = None
    tail: str = ""


class CancelJobResult(BaseModel):
    job_id: str
    state: JobState
    cancellation_requested: bool
    terminal: bool


@dataclass
class OutputTracker:
    max_log_bytes: int
    success_contains: str | None
    failure_contains: str | None
    last_output_at: float
    bytes_seen: int = 0
    bytes_written: int = 0
    log_truncated: bool = False
    tail_text: str = ""
    scan_text: str = ""
    success_match: str | None = None
    failure_match: str | None = None
    heartbeat_reports_completed: int = 0
    heartbeat_error: str | None = None

    def feed_text(self, text: str, raw_byte_count: int) -> None:
        self.last_output_at = time.monotonic()
        self.bytes_seen += raw_byte_count
        if not text:
            return
        self.tail_text = (self.tail_text + text)[-MAX_MEMORY_TAIL_CHARS:]
        self.scan_text = (self.scan_text + text)[-MAX_MEMORY_TAIL_CHARS:]
        if self.success_contains and self.success_match is None and self.success_contains in self.scan_text:
            self.success_match = self.success_contains
        if self.failure_contains and self.failure_match is None and self.failure_contains in self.scan_text:
            self.failure_match = self.failure_contains


TERMINAL_JOB_STATES = {
    "succeeded",
    "failed",
    "timed_out",
    "inactive_timeout",
    "success_condition_not_met",
    "spawn_error",
    "cancelled",
    "orphaned_recovered",
}
_BACKGROUND_JOBS: dict[str, asyncio.Task[RunResult]] = {}


WakePolicy = Literal["auto", "goal", "none"]


@dataclass(frozen=True)
class WakeRegistration:
    socket_path: str
    thread_id: str


def _thread_id_from_context(ctx: Context | None) -> str | None:
    if ctx is None:
        return None
    try:
        meta = ctx.request_context.meta
    except (AttributeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    thread_id = meta.get("threadId")
    return thread_id if isinstance(thread_id, str) else None


def _new_job_id() -> str:
    return uuid.uuid4().hex


def _metadata_path(job_id: str) -> Path:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("invalid job_id")
    return JOBS_DIR / f"{job_id}.json"


def _read_job_metadata(job_id: str) -> dict[str, object]:
    path = _metadata_path(job_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"no job metadata found for {job_id}") from exc
    if not isinstance(payload, dict) or payload.get("job_id") != job_id:
        raise ValueError(f"invalid metadata for job {job_id}")
    cwd = payload.get("cwd")
    if isinstance(cwd, str):
        resolved = Path(cwd).expanduser().resolve()
        if not any(_is_within(resolved, root) for root in ALLOWED_ROOTS):
            raise PermissionError(f"job {job_id} is outside the currently allowed roots")
    return payload


def _elapsed_from_payload(payload: dict[str, object]) -> float:
    stored = payload.get("duration_sec")
    if isinstance(stored, (int, float)):
        return round(float(stored), 3)
    started = payload.get("started_at_utc")
    if isinstance(started, str):
        try:
            parsed = datetime.fromisoformat(started)
            return round(max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds()), 3)
        except ValueError:
            pass
    return 0.0


def _job_status_from_payload(
    payload: dict[str, object],
    *,
    tail_lines: int = 80,
    tail_bytes: int = 16384,
) -> JobStatusResult:
    job_id = str(payload["job_id"])
    state = str(payload.get("state", "spawn_error"))
    terminal = state in TERMINAL_JOB_STATES
    tail = ""
    if terminal and tail_lines > 0:
        tail_path = JOBS_DIR / f"{job_id}.tail.txt"
        if tail_path.is_file():
            text = tail_path.read_text(encoding="utf-8", errors="replace")
            tail = _limit_tail(text, tail_lines, tail_bytes)
        elif isinstance(payload.get("tail"), str):
            tail = _limit_tail(str(payload["tail"]), tail_lines, tail_bytes)
    return JobStatusResult(
        job_id=job_id,
        state=state,  # type: ignore[arg-type]
        terminal=terminal,
        automatic_wakeup=bool(payload.get("automatic_wakeup", False)),
        wake_delivery=(
            str(payload["wake_delivery"]) if isinstance(payload.get("wake_delivery"), str) else None
        ),
        recommended_check_after_sec=(
            None if terminal or bool(payload.get("automatic_wakeup", False)) else 600
        ),
        exit_code=payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else None,
        duration_sec=_elapsed_from_payload(payload),
        started_at_utc=str(payload.get("started_at_utc", "")),
        finished_at_utc=(
            str(payload["finished_at_utc"]) if isinstance(payload.get("finished_at_utc"), str) else None
        ),
        command_display=str(payload.get("command_display", "<pending validation>")),
        cwd=str(payload.get("cwd", "")),
        log_path=str(payload.get("log_path", JOBS_DIR / f"{job_id}.log")),
        metadata_path=str(_metadata_path(job_id)),
        output_bytes_seen=int(payload.get("output_bytes_seen", 0)),
        output_bytes_logged=int(payload.get("output_bytes_logged", 0)),
        log_truncated=bool(payload.get("log_truncated", False)),
        success_match=(str(payload["success_match"]) if payload.get("success_match") is not None else None),
        failure_match=(str(payload["failure_match"]) if payload.get("failure_match") is not None else None),
        error=str(payload["error"]) if payload.get("error") is not None else None,
        tail=tail,
    )


def _active_job_count_locked() -> int:
    active = 0
    for path in JOBS_DIR.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("state") not in {"queued", "starting", "running"}:
            continue
        if _pid_matches(payload.get("server_pid"), payload.get("server_start_ticks")):
            active += 1
    return active


def _reserve_job(job_id: str, cwd: str) -> None:
    with _locked_job_state():
        active = _active_job_count_locked()
        if active >= MAX_ACTIVE_JOBS:
            raise RuntimeError(f"active job limit reached ({active}/{MAX_ACTIVE_JOBS})")
        _atomic_write_json(
            _metadata_path(job_id),
            {
                "job_id": job_id,
                "state": "queued",
                "started_at_utc": _utc_now(),
                "server_pid": os.getpid(),
                "server_start_ticks": SERVER_START_TICKS,
                "command_display": "<pending validation>",
                "cwd": cwd,
                "log_path": str(JOBS_DIR / f"{job_id}.log"),
            },
        )


def _update_job_metadata(job_id: str, **fields: object) -> None:
    path = _metadata_path(job_id)
    with _locked_job_state():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {"job_id": job_id, "started_at_utc": _utc_now(), "cwd": ""}
        if not isinstance(payload, dict):
            payload = {"job_id": job_id, "started_at_utc": _utc_now(), "cwd": ""}
        payload.update(fields)
        _atomic_write_json(path, payload)


async def _prepare_wake_registration(
    *,
    job_id: str,
    ctx: Context | None,
    wake_policy: WakePolicy,
    timeout_sec: int,
    grace_period_sec: int,
) -> tuple[WakeRegistration | None, str | None]:
    if wake_policy == "none":
        return None, None
    thread_id = _thread_id_from_context(ctx)
    if not BRIDGE_SOCKET or thread_id is None:
        if wake_policy == "goal":
            missing = "bridge socket" if not BRIDGE_SOCKET else "trusted Codex thread metadata"
            raise RuntimeError(f"Goal wakeup requested but {missing} is unavailable")
        return None, "bridge_unavailable"
    try:
        response = await request_bridge(
            BRIDGE_SOCKET,
            {
                "action": "prepare",
                "job_id": job_id,
                "thread_id": thread_id,
                "timeout_sec": timeout_sec,
                "grace_period_sec": grace_period_sec,
            },
        )
    except BridgeError as exc:
        # A transport failure can be ambiguous: prepare may have reached the
        # bridge and paused the Goal before the response was lost. Best-effort
        # abort the exact job/thread lease before either failing or falling back.
        try:
            await request_bridge(
                BRIDGE_SOCKET,
                {
                    "action": "abort",
                    "job_id": job_id,
                    "thread_id": thread_id,
                },
            )
        except BridgeError:
            pass
        if wake_policy == "goal":
            raise RuntimeError(f"Goal wakeup setup failed: {exc}") from exc
        return None, "bridge_rejected"
    if response.get("automatic_wakeup") is not True:
        if wake_policy == "goal":
            raise RuntimeError("Goal bridge did not confirm automatic wakeup")
        return None, "bridge_not_armed"
    return WakeRegistration(socket_path=BRIDGE_SOCKET, thread_id=thread_id), "armed"


async def _abort_wake_registration(job_id: str, registration: WakeRegistration) -> None:
    try:
        await request_bridge(
            registration.socket_path,
            {
                "action": "abort",
                "job_id": job_id,
                "thread_id": registration.thread_id,
            },
        )
    except BridgeError:
        _update_job_metadata(job_id, wake_delivery="abort_failed")


async def _deliver_terminal_wakeup(job_id: str, registration: WakeRegistration) -> None:
    try:
        payload = _read_job_metadata(job_id)
    except Exception:
        return
    state = str(payload.get("state", ""))
    if state not in TERMINAL_JOB_STATES:
        return
    try:
        response = await request_bridge(
            registration.socket_path,
            {
                "action": "terminal",
                "job_id": job_id,
                "thread_id": registration.thread_id,
                "terminal_state": state,
            },
        )
        delivery = response.get("delivery_state")
        _update_job_metadata(
            job_id,
            wake_delivery=str(delivery) if isinstance(delivery, str) else "accepted",
        )
    except BridgeError as exc:
        _update_job_metadata(
            job_id,
            wake_delivery="delivery_failed",
            wake_error=f"{type(exc).__name__}: {exc}",
        )


async def _run_background_job(
    job_id: str,
    argv: list[str],
    cwd: str,
    timeout_sec: int,
    no_output_timeout_sec: int,
    grace_period_sec: int,
    success_contains: str | None,
    failure_contains: str | None,
    tail_lines: int,
    tail_bytes: int,
    ready_event: asyncio.Event,
    registration: WakeRegistration | None,
    wake_metadata: dict[str, object],
) -> RunResult:
    try:
        return await _execute_job(
            job_id,
            argv,
            cwd,
            timeout_sec,
            no_output_timeout_sec,
            grace_period_sec,
            success_contains,
            failure_contains,
            tail_lines,
            tail_bytes,
            None,
            ready_event,
            wake_metadata,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _update_job_metadata(
            job_id,
            state="spawn_error",
            finished_at_utc=_utc_now(),
            error=f"background task error: {type(exc).__name__}: {exc}",
            **wake_metadata,
        )
        raise
    finally:
        if registration is not None:
            await _deliver_terminal_wakeup(job_id, registration)


def _consume_background_result(job_id: str, task: asyncio.Task[RunResult]) -> None:
    _BACKGROUND_JOBS.pop(job_id, None)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        path = JOBS_DIR / f"{job_id}.json"
        with _locked_job_state():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {"job_id": job_id, "started_at_utc": _utc_now(), "cwd": ""}
            payload.update(
                {
                    "state": "spawn_error",
                    "finished_at_utc": _utc_now(),
                    "error": f"background task error: {type(exc).__name__}: {exc}",
                }
            )
            _atomic_write_json(path, payload)


def _mcp_version() -> str:
    try:
        return package_version("mcp")
    except PackageNotFoundError:
        return "unknown"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_cwd(cwd: str) -> Path:
    candidate = Path(cwd).expanduser()
    if not candidate.is_absolute():
        raise ValueError("cwd must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"cwd does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise ValueError(f"cwd is not a directory: {resolved}")
    if not any(_is_within(resolved, root) for root in ALLOWED_ROOTS):
        allowed = os.pathsep.join(str(root) for root in ALLOWED_ROOTS)
        raise ValueError(
            f"cwd {resolved} is outside LONGRUN_ALLOWED_ROOTS ({allowed}); "
            "this check is a routing guard, not a sandbox"
        )
    return resolved


def _validate_argv(argv: list[str], cwd: Path) -> list[str]:
    if not argv:
        raise ValueError("argv must contain at least one element")
    if len(argv) > 256:
        raise ValueError("argv contains more than 256 elements")
    total = 0
    validated: list[str] = []
    for index, item in enumerate(argv):
        if not isinstance(item, str):
            raise ValueError(f"argv[{index}] is not a string")
        if not item:
            raise ValueError(f"argv[{index}] is empty")
        if "\x00" in item:
            raise ValueError(f"argv[{index}] contains a NUL byte")
        total += len(item)
        if total > 1024 * 1024:
            raise ValueError("combined argv length exceeds 1 MiB")
        validated.append(item)

    executable_names = {Path(validated[0]).name.lower()}
    executable_path: Path | None = None
    if os.sep in validated[0]:
        candidate = Path(validated[0])
        executable_path = candidate if candidate.is_absolute() else cwd / candidate
    else:
        found = shutil.which(validated[0], path=os.environ.get("PATH"))
        if found:
            executable_path = Path(found)
    if executable_path is not None:
        try:
            executable_names.add(executable_path.resolve(strict=True).name.lower())
        except (FileNotFoundError, OSError):
            pass
    blocked_elevation = executable_names & ELEVATION_EXECUTABLES
    if blocked_elevation:
        raise ValueError(f"privilege-elevation command is disabled: {sorted(blocked_elevation)[0]}")
    blocked_shell = executable_names & SHELL_EXECUTABLES
    if not ALLOW_SHELL and blocked_shell:
        raise ValueError(f"shell execution is disabled: {sorted(blocked_shell)[0]}")
    return validated


_SECRET_FLAG_PARTS = ("password", "passwd", "token", "secret", "api-key", "apikey", "auth")


def _redact_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        lower = arg.lower()
        if "=" in arg:
            name, _value = arg.split("=", 1)
            if any(part in name.lower() for part in _SECRET_FLAG_PARTS):
                redacted.append(f"{name}=<redacted>")
                continue
        redacted.append(arg)
        if lower.startswith("-") and any(part in lower for part in _SECRET_FLAG_PARTS):
            redact_next = True
    return redacted


def _command_display(argv: list[str]) -> str:
    return shlex.join(_redact_argv(argv))


def _argv_digest(argv: list[str]) -> str:
    encoded = json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_process_env(cwd: Path) -> dict[str, str]:
    result = {name: os.environ[name] for name in FORWARD_ENV_NAMES if name in os.environ}
    result["PWD"] = str(cwd)
    return result


def _validate_match_text(value: str | None, field_name: str) -> str | None:
    if value is None or value == "":
        return None
    if len(value) > MAX_MATCH_TEXT_LENGTH:
        raise ValueError(f"{field_name} exceeds {MAX_MATCH_TEXT_LENGTH} characters")
    return value


def _limit_tail(text: str, max_lines: int, max_bytes: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        encoded = encoded[-max_bytes:]
        text = encoded.decode("utf-8", errors="replace")
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1 :]
    if max_lines == 0:
        return ""
    return "\n".join(text.splitlines()[-max_lines:])


async def _drain_output(stream: asyncio.StreamReader, log_path: Path, tracker: OutputTracker) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    with log_path.open("wb") as log_file:
        log_path.chmod(0o600)
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            text = decoder.decode(chunk, final=False)
            tracker.feed_text(text, len(chunk))
            remaining = tracker.max_log_bytes - tracker.bytes_written
            if remaining > 0:
                part = chunk[:remaining]
                log_file.write(part)
                log_file.flush()
                tracker.bytes_written += len(part)
            if len(chunk) > max(remaining, 0):
                tracker.log_truncated = True
        final_text = decoder.decode(b"", final=True)
        if final_text:
            tracker.feed_text(final_text, 0)


async def _terminate_process(proc: asyncio.subprocess.Process, grace_period_sec: int) -> None:
    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            # The supervisor applies the requested grace period to the command group.
            await asyncio.wait_for(proc.wait(), timeout=grace_period_sec + 2)
        except asyncio.TimeoutError:
            pass

    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def _read_command_pgid(status_read_fd: int) -> int:
    loop = asyncio.get_running_loop()
    pipe = os.fdopen(status_read_fd, "rb", buffering=0)
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    try:
        transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
    except BaseException:
        pipe.close()
        raise
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=2)
    finally:
        transport.close()
    try:
        command_pgid = int(line.strip())
    except ValueError as exc:
        raise RuntimeError(f"supervisor returned an invalid command PGID: {line!r}") from exc
    if command_pgid <= 0:
        raise RuntimeError(f"supervisor returned a non-positive command PGID: {command_pgid}")
    return command_pgid


async def _wait_with_limits(
    proc: asyncio.subprocess.Process,
    tracker: OutputTracker,
    started_monotonic: float,
    timeout_sec: int,
    no_output_timeout_sec: int,
    progress_reporter: Callable[[float, str], Awaitable[None]] | None = None,
) -> Literal["timed_out", "inactive_timeout"] | None:
    deadline = started_monotonic + timeout_sec
    next_heartbeat = (
        started_monotonic + HEARTBEAT_INITIAL_SEC
        if progress_reporter is not None and HEARTBEAT_INITIAL_SEC > 0
        else None
    )
    while proc.returncode is None:
        now = time.monotonic()
        if now >= deadline:
            return "timed_out"
        if no_output_timeout_sec > 0 and now - tracker.last_output_at >= no_output_timeout_sec:
            return "inactive_timeout"
        if next_heartbeat is not None and now >= next_heartbeat:
            elapsed = max(0.0, now - started_monotonic)
            if tracker.bytes_seen > 0:
                output_status = f"last output {_format_duration(now - tracker.last_output_at)} ago"
            else:
                output_status = "no output yet"
            message = (
                f"Still running: {_format_duration(elapsed)} elapsed; {output_status}; "
                f"{_format_bytes(tracker.bytes_seen)} captured."
            )
            try:
                await asyncio.wait_for(progress_reporter(elapsed, message), timeout=2.0)
                tracker.heartbeat_reports_completed += 1
                next_heartbeat += HEARTBEAT_INTERVAL_SEC
                while next_heartbeat <= time.monotonic():
                    next_heartbeat += HEARTBEAT_INTERVAL_SEC
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                tracker.heartbeat_error = type(exc).__name__
                next_heartbeat = None
            continue
        next_deadline = deadline
        if no_output_timeout_sec > 0:
            next_deadline = min(next_deadline, tracker.last_output_at + no_output_timeout_sec)
        if next_heartbeat is not None:
            next_deadline = min(next_deadline, next_heartbeat)
        await asyncio.sleep(max(0.05, min(1.0, next_deadline - now)))
    return None


def _format_duration(seconds: float) -> str:
    whole_seconds = max(0, int(seconds))
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_bytes(byte_count: int) -> str:
    value = float(max(0, byte_count))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _result_state(
    stop_reason: Literal["timed_out", "inactive_timeout"] | None,
    exit_code: int | None,
    tracker: OutputTracker,
) -> RunState:
    if stop_reason == "timed_out":
        return "timed_out"
    if stop_reason == "inactive_timeout":
        return "inactive_timeout"
    if exit_code != 0 or tracker.failure_match is not None:
        return "failed"
    if tracker.success_contains is not None and tracker.success_match is None:
        return "success_condition_not_met"
    return "succeeded"


@mcp.tool(
    title="Check longrun MCP health",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def health() -> HealthResult:
    """Return server paths, versions, and configured guardrails."""
    _ensure_state_dirs()
    return HealthResult(
        ok=True,
        server_version=SERVER_VERSION,
        mcp_version=_mcp_version(),
        python_version=sys.version.split()[0],
        state_dir=str(STATE_DIR),
        allowed_roots=[str(path) for path in ALLOWED_ROOTS],
        forwarded_env_names=list(FORWARD_ENV_NAMES),
        allow_shell=ALLOW_SHELL,
        max_log_bytes=MAX_LOG_BYTES,
        max_timeout_sec=MAX_TIMEOUT_SEC,
        max_active_jobs=MAX_ACTIVE_JOBS,
        heartbeat_initial_sec=HEARTBEAT_INITIAL_SEC,
        heartbeat_interval_sec=HEARTBEAT_INTERVAL_SEC,
        recovered_orphan_jobs=RECOVERED_ORPHAN_JOBS,
        bridge_configured=bool(BRIDGE_SOCKET),
    )


async def _execute_job(
    job_id: str,
    argv: list[str],
    cwd: str,
    timeout_sec: int,
    no_output_timeout_sec: int,
    grace_period_sec: int,
    success_contains: str | None,
    failure_contains: str | None,
    tail_lines: int,
    tail_bytes: int,
    ctx: Context | None,
    ready_event: asyncio.Event | None = None,
    wake_metadata: dict[str, object] | None = None,
) -> RunResult:
    """Execute one validated job and persist a bounded terminal result."""
    _ensure_state_dirs()
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    log_path = JOBS_DIR / f"{job_id}.log"
    tail_path = JOBS_DIR / f"{job_id}.tail.txt"
    metadata_path = JOBS_DIR / f"{job_id}.json"
    wake_metadata = dict(wake_metadata or {})

    try:
        resolved_cwd = _resolve_cwd(cwd)
        validated_argv = _validate_argv(argv, resolved_cwd)
        if timeout_sec > MAX_TIMEOUT_SEC:
            raise ValueError(f"timeout_sec exceeds configured maximum {MAX_TIMEOUT_SEC}")
        success_contains = _validate_match_text(success_contains, "success_contains")
        failure_contains = _validate_match_text(failure_contains, "failure_contains")
        process_env = _build_process_env(resolved_cwd)
    except Exception as exc:
        result = RunResult(
            job_id=job_id,
            state="spawn_error",
            exit_code=None,
            duration_sec=round(time.monotonic() - started_monotonic, 3),
            started_at_utc=started_at,
            finished_at_utc=_utc_now(),
            command_display="<rejected command>",
            cwd=cwd,
            log_path=str(log_path),
            metadata_path=str(metadata_path),
            output_bytes_seen=0,
            output_bytes_logged=0,
            log_truncated=False,
            error=f"validation error: {exc}",
        )
        _atomic_write_json(metadata_path, {**result.model_dump(), **wake_metadata})
        if ready_event is not None:
            ready_event.set()
        return result

    command_display = _command_display(validated_argv)
    initial_metadata: dict[str, object] = {
        "job_id": job_id,
        "state": "starting",
        "started_at_utc": started_at,
        "server_pid": os.getpid(),
        "server_start_ticks": SERVER_START_TICKS,
        "argv_sha256": _argv_digest(validated_argv),
        "command_display": command_display,
        "cwd": str(resolved_cwd),
        "log_path": str(log_path),
        "timeout_sec": timeout_sec,
        "no_output_timeout_sec": no_output_timeout_sec,
        "heartbeat_initial_sec": HEARTBEAT_INITIAL_SEC,
        "heartbeat_interval_sec": HEARTBEAT_INTERVAL_SEC,
        "forwarded_env_names": [name for name in FORWARD_ENV_NAMES if name in process_env],
        **wake_metadata,
    }
    _atomic_write_json(metadata_path, initial_metadata)
    _atomic_write_text(STATE_DIR / "latest-log-path", str(log_path) + "\n")

    status_read_fd, status_write_fd = os.pipe()
    runner_argv = [
        sys.executable,
        "-m",
        "codex_mcp_longrun.job_exec",
        str(os.getpid()),
        str(grace_period_sec),
        str(status_write_fd),
        *validated_argv,
    ]
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *runner_argv,
                cwd=str(resolved_cwd),
                env=process_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                pass_fds=(status_write_fd,),
            )
        finally:
            os.close(status_write_fd)
    except Exception as exc:
        os.close(status_read_fd)
        result = RunResult(
            job_id=job_id,
            state="spawn_error",
            exit_code=None,
            duration_sec=round(time.monotonic() - started_monotonic, 3),
            started_at_utc=started_at,
            finished_at_utc=_utc_now(),
            command_display=command_display,
            cwd=str(resolved_cwd),
            log_path=str(log_path),
            metadata_path=str(metadata_path),
            output_bytes_seen=0,
            output_bytes_logged=0,
            log_truncated=False,
            error=f"spawn error: {type(exc).__name__}: {exc}",
        )
        _atomic_write_json(metadata_path, {**result.model_dump(), **wake_metadata})
        if ready_event is not None:
            ready_event.set()
        return result

    if proc.stdout is None:
        os.close(status_read_fd)
        await _terminate_process(proc, grace_period_sec)
        raise RuntimeError("subprocess stdout pipe was not created")

    try:
        command_pgid = await _read_command_pgid(status_read_fd)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await _terminate_process(proc, grace_period_sec)
            _atomic_write_json(
                metadata_path,
                {
                    **initial_metadata,
                    "state": "cancelled",
                    "finished_at_utc": _utc_now(),
                    "duration_sec": round(time.monotonic() - started_monotonic, 3),
                    "exit_code": proc.returncode,
                    "error": "cancelled while waiting for supervisor startup",
                    **wake_metadata,
                },
            )
            if ready_event is not None:
                ready_event.set()
        raise
    except Exception as exc:
        await _terminate_process(proc, grace_period_sec)
        result = RunResult(
            job_id=job_id,
            state="spawn_error",
            exit_code=proc.returncode,
            duration_sec=round(time.monotonic() - started_monotonic, 3),
            started_at_utc=started_at,
            finished_at_utc=_utc_now(),
            command_display=command_display,
            cwd=str(resolved_cwd),
            log_path=str(log_path),
            metadata_path=str(metadata_path),
            output_bytes_seen=0,
            output_bytes_logged=0,
            log_truncated=False,
            error=f"supervisor startup error: {type(exc).__name__}: {exc}",
        )
        _atomic_write_json(metadata_path, {**result.model_dump(), **wake_metadata})
        if ready_event is not None:
            ready_event.set()
        return result

    initial_metadata.update(
        {
            "state": "running",
            "pid": proc.pid,
            "process_start_ticks": _proc_start_ticks(proc.pid),
            "command_pgid": command_pgid,
            "command_start_ticks": _proc_start_ticks(command_pgid),
        }
    )
    _atomic_write_json(metadata_path, initial_metadata)
    if ready_event is not None:
        ready_event.set()
    tracker = OutputTracker(
        max_log_bytes=MAX_LOG_BYTES,
        success_contains=success_contains,
        failure_contains=failure_contains,
        last_output_at=started_monotonic,
    )
    reader_task = asyncio.create_task(_drain_output(proc.stdout, log_path, tracker))
    stop_reason: Literal["timed_out", "inactive_timeout"] | None = None

    async def report_progress(progress: float, message: str) -> None:
        if ctx is not None:
            await ctx.report_progress(progress=progress, total=None, message=message)

    try:
        stop_reason = await _wait_with_limits(
            proc,
            tracker,
            started_monotonic,
            timeout_sec,
            no_output_timeout_sec,
            report_progress if ctx is not None else None,
        )
        if stop_reason is not None:
            await _terminate_process(proc, grace_period_sec)
        exit_code = await proc.wait()
        await reader_task
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await _terminate_process(proc, grace_period_sec)
            try:
                await reader_task
            except BaseException:
                pass
            _atomic_write_text(tail_path, tracker.tail_text)
            _atomic_write_json(
                metadata_path,
                {
                    **initial_metadata,
                    "state": "cancelled",
                    "finished_at_utc": _utc_now(),
                    "duration_sec": round(time.monotonic() - started_monotonic, 3),
                    "exit_code": proc.returncode,
                    "output_bytes_seen": tracker.bytes_seen,
                    "output_bytes_logged": tracker.bytes_written,
                    "log_truncated": tracker.log_truncated,
                    "heartbeat_reports_completed": tracker.heartbeat_reports_completed,
                    "heartbeat_error": tracker.heartbeat_error,
                    "tail_path": str(tail_path),
                    **wake_metadata,
                },
            )
        raise
    except Exception as exc:
        await _terminate_process(proc, grace_period_sec)
        if not reader_task.done():
            reader_task.cancel()
        try:
            await reader_task
        except BaseException:
            pass
        _atomic_write_text(tail_path, tracker.tail_text)
        result = RunResult(
            job_id=job_id,
            state="spawn_error",
            exit_code=proc.returncode,
            duration_sec=round(time.monotonic() - started_monotonic, 3),
            started_at_utc=started_at,
            finished_at_utc=_utc_now(),
            command_display=command_display,
            cwd=str(resolved_cwd),
            log_path=str(log_path),
            metadata_path=str(metadata_path),
            output_bytes_seen=tracker.bytes_seen,
            output_bytes_logged=tracker.bytes_written,
            log_truncated=tracker.log_truncated,
            success_match=tracker.success_match,
            failure_match=tracker.failure_match,
            error=f"runtime error: {type(exc).__name__}: {exc}",
            tail=_limit_tail(tracker.tail_text, tail_lines, tail_bytes),
        )
        _atomic_write_json(metadata_path, {**result.model_dump(), **wake_metadata})
        if ready_event is not None:
            ready_event.set()
        return result

    _atomic_write_text(tail_path, tracker.tail_text)
    with _locked_job_state():
        try:
            current_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current_metadata = {}
        cancellation_requested = bool(
            isinstance(current_metadata, dict) and current_metadata.get("cancel_requested_at_utc")
        )
        state: RunState = "cancelled" if cancellation_requested else _result_state(stop_reason, exit_code, tracker)
        result = RunResult(
            job_id=job_id,
            state=state,
            exit_code=exit_code,
            duration_sec=round(time.monotonic() - started_monotonic, 3),
            started_at_utc=started_at,
            finished_at_utc=_utc_now(),
            command_display=command_display,
            cwd=str(resolved_cwd),
            log_path=str(log_path),
            metadata_path=str(metadata_path),
            output_bytes_seen=tracker.bytes_seen,
            output_bytes_logged=tracker.bytes_written,
            log_truncated=tracker.log_truncated,
            success_match=tracker.success_match,
            failure_match=tracker.failure_match,
            tail=_limit_tail(tracker.tail_text, tail_lines, tail_bytes),
        )
        metadata = result.model_dump()
        metadata.update(
            {
                "pid": proc.pid,
                "process_start_ticks": initial_metadata.get("process_start_ticks"),
                "command_pgid": initial_metadata.get("command_pgid"),
                "command_start_ticks": initial_metadata.get("command_start_ticks"),
                "server_pid": os.getpid(),
                "server_start_ticks": SERVER_START_TICKS,
                "argv_sha256": initial_metadata["argv_sha256"],
                "tail_path": str(tail_path),
                "forwarded_env_names": initial_metadata["forwarded_env_names"],
                "heartbeat_reports_completed": tracker.heartbeat_reports_completed,
                "heartbeat_error": tracker.heartbeat_error,
                **wake_metadata,
            }
        )
        if cancellation_requested and isinstance(current_metadata, dict):
            metadata["cancel_requested_at_utc"] = current_metadata["cancel_requested_at_utc"]
        _atomic_write_json(metadata_path, metadata)
    return result


@mcp.tool(
    title="Start a bounded long-running command",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def start_job(
    argv: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=256,
            description="Command and arguments as an array. Shell and privilege-elevation commands are disabled.",
        ),
    ],
    cwd: Annotated[str, Field(description="Absolute working directory inside LONGRUN_ALLOWED_ROOTS.")],
    timeout_sec: Annotated[int, Field(ge=1, le=43200)] = 3600,
    no_output_timeout_sec: Annotated[int, Field(ge=0, le=43200)] = 0,
    grace_period_sec: Annotated[int, Field(ge=1, le=120)] = 10,
    success_contains: Annotated[str | None, Field(max_length=MAX_MATCH_TEXT_LENGTH)] = None,
    failure_contains: Annotated[str | None, Field(max_length=MAX_MATCH_TEXT_LENGTH)] = None,
    tail_lines: Annotated[int, Field(ge=0, le=200)] = 80,
    tail_bytes: Annotated[int, Field(ge=1024, le=65536)] = 16384,
    wake_policy: Annotated[
        WakePolicy,
        Field(
            description=(
                "auto uses a configured Goal bridge when possible; goal requires it; "
                "none always returns without automatic wakeup."
            )
        ),
    ] = "auto",
    ctx: Context | None = None,
) -> JobStatusResult:
    """Start one approved job and optionally arm event-driven Goal wakeup."""
    job_id = _new_job_id()
    try:
        resolved_cwd = _resolve_cwd(cwd)
        _validate_argv(argv, resolved_cwd)
        if timeout_sec > MAX_TIMEOUT_SEC:
            raise ValueError(f"timeout_sec exceeds configured maximum {MAX_TIMEOUT_SEC}")
        _validate_match_text(success_contains, "success_contains")
        _validate_match_text(failure_contains, "failure_contains")
    except Exception:
        resolved_cwd = None
    if resolved_cwd is not None:
        _reserve_job(job_id, str(resolved_cwd))

    registration: WakeRegistration | None = None
    wake_delivery: str | None = None
    if resolved_cwd is not None:
        try:
            registration, wake_delivery = await _prepare_wake_registration(
                job_id=job_id,
                ctx=ctx,
                wake_policy=wake_policy,
                timeout_sec=timeout_sec,
                grace_period_sec=grace_period_sec,
            )
        except Exception as exc:
            _update_job_metadata(
                job_id,
                state="spawn_error",
                finished_at_utc=_utc_now(),
                automatic_wakeup=False,
                wake_policy=wake_policy,
                wake_delivery="setup_failed",
                error=f"Goal wakeup setup failed before command start: {exc}",
            )
            raise

    wake_metadata: dict[str, object] = {
        "automatic_wakeup": registration is not None,
        "wake_policy": wake_policy,
    }
    if wake_delivery is not None:
        wake_metadata["wake_delivery"] = wake_delivery
    if registration is not None:
        wake_metadata["wake_thread_id"] = registration.thread_id
    if resolved_cwd is not None:
        _update_job_metadata(job_id, **wake_metadata)

    ready_event = asyncio.Event()
    try:
        task = asyncio.create_task(
            _run_background_job(
                job_id,
                argv,
                cwd,
                timeout_sec,
                no_output_timeout_sec,
                grace_period_sec,
                success_contains,
                failure_contains,
                tail_lines,
                tail_bytes,
                ready_event,
                registration,
                wake_metadata,
            ),
            name=f"codex-longrun-{job_id}",
        )
    except Exception:
        if registration is not None:
            await _abort_wake_registration(job_id, registration)
        raise
    _BACKGROUND_JOBS[job_id] = task
    task.add_done_callback(lambda completed, jid=job_id: _consume_background_result(jid, completed))
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        pass
    return _job_status_from_payload(_read_job_metadata(job_id), tail_lines=tail_lines, tail_bytes=tail_bytes)


@mcp.tool(
    title="Get one bounded longrun job status",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def get_job(
    job_id: Annotated[str, Field(pattern=r"^(?:[a-f0-9]{12}|[a-f0-9]{32})$")],
    tail_lines: Annotated[int, Field(ge=0, le=200)] = 80,
    tail_bytes: Annotated[int, Field(ge=1024, le=65536)] = 16384,
) -> JobStatusResult:
    """Read a job once; do not repeatedly poll a running job in the same turn."""
    return _job_status_from_payload(
        _read_job_metadata(job_id),
        tail_lines=tail_lines,
        tail_bytes=tail_bytes,
    )


@mcp.tool(
    title="Cancel a running longrun job",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
async def cancel_job(
    job_id: Annotated[str, Field(pattern=r"^(?:[a-f0-9]{12}|[a-f0-9]{32})$")],
) -> CancelJobResult:
    """Request cancellation of one exact job after approval."""
    with _locked_job_state():
        payload = _read_job_metadata(job_id)
        state = str(payload.get("state", "spawn_error"))
        if state in TERMINAL_JOB_STATES:
            return CancelJobResult(
                job_id=job_id,
                state=state,  # type: ignore[arg-type]
                cancellation_requested=False,
                terminal=True,
            )
        payload["cancel_requested_at_utc"] = _utc_now()
        _atomic_write_json(_metadata_path(job_id), payload)

    task = _BACKGROUND_JOBS.get(job_id)
    if task is not None:
        task.cancel()
    else:
        supervisor_pid = payload.get("pid")
        supervisor_ticks = payload.get("process_start_ticks")
        if _pid_matches(supervisor_pid, supervisor_ticks) and isinstance(supervisor_pid, int):
            try:
                os.kill(supervisor_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    return CancelJobResult(
        job_id=job_id,
        state=state,  # type: ignore[arg-type]
        cancellation_requested=True,
        terminal=False,
    )


@mcp.tool(
    title="Run a long command and wait locally (legacy compatibility)",
    annotations=ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
)
async def run_and_wait(
    argv: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=256,
            description="Legacy blocking mode. Prefer start_job in Codex.",
        ),
    ],
    cwd: Annotated[str, Field(description="Absolute working directory inside LONGRUN_ALLOWED_ROOTS.")],
    timeout_sec: Annotated[int, Field(ge=1, le=43200)] = 3600,
    no_output_timeout_sec: Annotated[int, Field(ge=0, le=43200)] = 0,
    grace_period_sec: Annotated[int, Field(ge=1, le=120)] = 10,
    success_contains: Annotated[str | None, Field(max_length=MAX_MATCH_TEXT_LENGTH)] = None,
    failure_contains: Annotated[str | None, Field(max_length=MAX_MATCH_TEXT_LENGTH)] = None,
    tail_lines: Annotated[int, Field(ge=0, le=200)] = 80,
    tail_bytes: Annotated[int, Field(ge=1024, le=65536)] = 16384,
    ctx: Context | None = None,
) -> RunResult:
    """Legacy blocking mode for clients that can await tools without model-driven polling."""
    job_id = _new_job_id()
    try:
        _reserve_job(job_id, str(_resolve_cwd(cwd)))
    except ValueError:
        pass
    return await _execute_job(
        job_id,
        argv,
        cwd,
        timeout_sec,
        no_output_timeout_sec,
        grace_period_sec,
        success_contains,
        failure_contains,
        tail_lines,
        tail_bytes,
        ctx,
    )


@mcp.tool(
    title="Read a bounded tail from a longrun job",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
async def read_log_tail(
    job_id: Annotated[str, Field(pattern=r"^(?:[a-f0-9]{12}|[a-f0-9]{32})$")],
    tail_lines: Annotated[int, Field(ge=1, le=200)] = 80,
    tail_bytes: Annotated[int, Field(ge=1024, le=65536)] = 16384,
) -> LogTailResult:
    """Read only the bounded tail stored for a known job ID."""
    _ensure_state_dirs()
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("invalid job_id")
    _read_job_metadata(job_id)
    tail_path = JOBS_DIR / f"{job_id}.tail.txt"
    log_path = JOBS_DIR / f"{job_id}.log"
    source = tail_path if tail_path.is_file() else log_path
    if not source.is_file():
        raise FileNotFoundError(f"no log found for job {job_id}")
    if source == tail_path:
        text = source.read_text(encoding="utf-8", errors="replace")
    else:
        with source.open("rb") as handle:
            size = source.stat().st_size
            handle.seek(max(0, size - tail_bytes))
            text = handle.read(tail_bytes).decode("utf-8", errors="replace")
    return LogTailResult(
        job_id=job_id,
        source_path=str(source),
        tail=_limit_tail(text, tail_lines, tail_bytes),
    )


def _write_all_stdout(data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(1, view)
        view = view[written:]


@asynccontextmanager
async def _stdio_transport():
    """MCP line transport that avoids AnyIO's worker-thread file wrapper."""
    read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_reader = anyio.create_memory_object_stream[SessionMessage](0)

    loop = asyncio.get_running_loop()
    stdin_reader = asyncio.StreamReader()
    stdin_protocol = asyncio.StreamReaderProtocol(stdin_reader)
    stdin_transport, _ = await loop.connect_read_pipe(lambda: stdin_protocol, sys.stdin.buffer)
    _debug_stdio("transport connected")

    async def read_stdin() -> None:
        async with read_writer:
            while line := await stdin_reader.readline():
                _debug_stdio(f"read {len(line)} bytes")
                try:
                    message = mcp_types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                except Exception as exc:
                    await read_writer.send(exc)
                    continue
                await read_writer.send(SessionMessage(message))
            _debug_stdio("stdin EOF")

    async def write_stdout() -> None:
        async with write_reader:
            async for session_message in write_reader:
                payload = session_message.message.model_dump_json(by_alias=True, exclude_unset=True)
                _debug_stdio(f"write {len(payload)} bytes")
                await asyncio.to_thread(_write_all_stdout, (payload + "\n").encode("utf-8"))

    try:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(read_stdin)
            task_group.start_soon(write_stdout)
            _debug_stdio("transport tasks started")
            yield read_stream, write_stream
            task_group.cancel_scope.cancel()
    finally:
        stdin_transport.close()


async def _run_stdio() -> None:
    async with _stdio_transport() as (read_stream, write_stream):
        _debug_stdio("starting low-level server")
        lowlevel_server = mcp._lowlevel_server
        await lowlevel_server.run(
            read_stream,
            write_stream,
            lowlevel_server.create_initialization_options(),
        )


def main() -> None:
    anyio.run(_run_stdio)


if __name__ == "__main__":
    main()
