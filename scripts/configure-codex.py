#!/usr/bin/env python3
"""Append a guarded longrun MCP entry to an existing Codex config."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path


SAFE_FORWARDED_ENV = (
    "HOME",
    "USER",
    "LOGNAME",
    "PATH",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _absolute_existing_dir(raw: str, name: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"{name} must be absolute: {path}")
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise SystemExit(f"{name} is not a directory: {path}")
    return path


def _render_block(command: Path, server_cwd: Path, state_dir: Path, allowed_root: Path) -> str:
    forwarded = [name for name in SAFE_FORWARDED_ENV if name in os.environ]
    forwarded_toml = ", ".join(_toml_string(name) for name in forwarded)
    forwarded_csv = ",".join(SAFE_FORWARDED_ENV)
    return f"""

# Local bounded long-running command MCP. Installed by codex-mcp-longrun.
[mcp_servers.longrun]
command = {_toml_string(str(command))}
args = []
cwd = {_toml_string(str(server_cwd))}
enabled = true
required = false
startup_timeout_sec = 20
tool_timeout_sec = 43500
enabled_tools = ["health", "run_and_wait", "read_log_tail"]
default_tools_approval_mode = "prompt"
env_vars = [{forwarded_toml}]

[mcp_servers.longrun.env]
LONGRUN_STATE_DIR = {_toml_string(str(state_dir))}
LONGRUN_ALLOWED_ROOTS = {_toml_string(str(allowed_root))}
LONGRUN_MAX_LOG_BYTES = "134217728"
LONGRUN_MAX_TIMEOUT_SEC = "43200"
LONGRUN_ALLOW_SHELL = "0"
LONGRUN_FORWARD_ENV_NAMES = {_toml_string(forwarded_csv)}

[mcp_servers.longrun.tools.health]
approval_mode = "auto"

[mcp_servers.longrun.tools.read_log_tail]
approval_mode = "prompt"

[mcp_servers.longrun.tools.run_and_wait]
approval_mode = "prompt"
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path.home() / ".codex" / "config.toml"))
    parser.add_argument("--command", required=True)
    parser.add_argument("--server-cwd", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--allowed-root", required=True)
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve(strict=True)
    if not config_path.is_file():
        raise SystemExit(f"config is not a regular file: {config_path}")
    command = Path(args.command).expanduser().resolve(strict=True)
    if not command.is_file() or not os.access(command, os.X_OK):
        raise SystemExit(f"server command is not executable: {command}")
    server_cwd = _absolute_existing_dir(args.server_cwd, "server cwd")
    allowed_root = _absolute_existing_dir(args.allowed_root, "allowed root")
    state_dir = Path(args.state_dir).expanduser()
    if not state_dir.is_absolute():
        raise SystemExit(f"state dir must be absolute: {state_dir}")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir = state_dir.resolve(strict=True)
    state_dir.chmod(0o700)

    original = config_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(original)
    if "longrun" in parsed.get("mcp_servers", {}):
        raise SystemExit("mcp_servers.longrun already exists; refusing to overwrite it")

    updated = original.rstrip() + _render_block(command, server_cwd, state_dir, allowed_root)
    tomllib.loads(updated)

    backup_dir = config_path.parent / "backups"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup_dir.chmod(0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"config.toml.before-longrun-{timestamp}"
    shutil.copy2(config_path, backup_path)
    backup_path.chmod(0o600)

    fd, temporary_name = tempfile.mkstemp(prefix=".config.toml.longrun-", dir=config_path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(config_path.stat().st_mode & 0o777)
        os.replace(temporary_path, config_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    print(f"Updated: {config_path}")
    print(f"Backup: {backup_path}")


if __name__ == "__main__":
    main()
