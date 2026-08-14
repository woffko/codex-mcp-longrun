#!/usr/bin/env python3
"""Safely upgrade an existing Codex longrun MCP configuration."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import stat
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_TOOLS = ["health", "start_job", "get_job", "cancel_job", "read_log_tail"]
TOOL_APPROVALS = {
    "start_job": "prompt",
    "get_job": "auto",
    "cancel_job": "prompt",
}


def _section_bounds(lines: list[str], section: str) -> tuple[int, int]:
    pattern = re.compile(rf"^\s*\[{re.escape(section)}\]\s*(?:#.*)?$")
    matches = [index for index, line in enumerate(lines) if pattern.fullmatch(line.rstrip("\r\n"))]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one [{section}] section, found {len(matches)}")
    start = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].lstrip().startswith("["):
            end = index
            break
    return start, end


def _render_value(value: object) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(item) for item in value) + "]"
    return json.dumps(value)


def _set_assignment(
    original: str,
    section: str,
    key: str,
    value: object,
    *,
    preserve_existing: bool = False,
) -> str:
    lines = original.splitlines(keepends=True)
    start, end = _section_bounds(lines, section)
    assignment = re.compile(rf"^\s*{re.escape(key)}\s*=")
    matches = [index for index in range(start + 1, end) if assignment.match(lines[index])]
    if len(matches) > 1:
        raise SystemExit(f"expected at most one {key} assignment in [{section}], found {len(matches)}")
    if matches and preserve_existing:
        return original
    rendered = f"{key} = {_render_value(value)}\n"
    if matches:
        index = matches[0]
        newline = "\r\n" if lines[index].endswith("\r\n") else "\n"
        if not lines[index].endswith(("\n", "\r")):
            newline = ""
        lines[index] = rendered.rstrip("\n") + newline
    else:
        lines.insert(end, rendered)
    return "".join(lines)


def _ensure_tool_section(original: str, server_name: str, tool: str, approval: str) -> str:
    section = f"mcp_servers.{server_name}.tools.{tool}"
    pattern = re.compile(rf"^\s*\[{re.escape(section)}\]\s*(?:#.*)?$", re.MULTILINE)
    if pattern.search(original):
        return original
    return original.rstrip() + f"\n\n[{section}]\napproval_mode = {json.dumps(approval)}\n"


def _write_config(config_path: Path, updated: str) -> Path:
    backup_dir = config_path.parent / "backups"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup_dir.chmod(0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{config_path.name}.before-longrun-upgrade-{timestamp}"
    shutil.copy2(config_path, backup_path)
    backup_path.chmod(0o600)

    fd, temporary_name = tempfile.mkstemp(prefix=f".{config_path.name}.longrun-upgrade-", dir=config_path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(stat.S_IMODE(config_path.stat().st_mode))
        os.replace(temporary_path, config_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    if config_path.read_text(encoding="utf-8") != updated:
        raise RuntimeError("config verification failed after atomic replacement")
    return backup_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Upgrade an existing Codex longrun MCP configuration.")
    parser.add_argument("--config", default=str(Path.home() / ".codex" / "config.toml"))
    parser.add_argument("--server-name", default="longrun")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reset-heartbeat",
        action="store_true",
        help="Set an existing heartbeat initial delay to zero instead of preserving it.",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve(strict=True)
    if not config_path.is_file():
        raise SystemExit(f"config is not a regular file: {config_path}")
    original = config_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(original)
    try:
        server = parsed["mcp_servers"][args.server_name]
        env = server["env"]
    except (KeyError, TypeError) as exc:
        raise SystemExit(f"mcp_servers.{args.server_name} is not a configured STDIO server") from exc
    if not isinstance(server, dict) or not isinstance(env, dict):
        raise SystemExit(f"mcp_servers.{args.server_name} has an invalid structure")

    updated = _set_assignment(original, f"mcp_servers.{args.server_name}", "enabled_tools", DEFAULT_TOOLS)
    updated = _set_assignment(
        updated,
        f"mcp_servers.{args.server_name}.env",
        "LONGRUN_HEARTBEAT_INITIAL_SEC",
        "0",
        preserve_existing=not args.reset_heartbeat,
    )
    updated = _set_assignment(
        updated,
        f"mcp_servers.{args.server_name}.env",
        "LONGRUN_MAX_ACTIVE_JOBS",
        "4",
        preserve_existing=True,
    )
    for tool, approval in TOOL_APPROVALS.items():
        updated = _ensure_tool_section(updated, args.server_name, tool, approval)

    updated_parsed = tomllib.loads(updated)
    expected = copy.deepcopy(parsed)
    expected_server = expected["mcp_servers"][args.server_name]
    expected_server["enabled_tools"] = DEFAULT_TOOLS
    expected_env = expected_server["env"]
    if "LONGRUN_HEARTBEAT_INITIAL_SEC" not in expected_env or args.reset_heartbeat:
        expected_env["LONGRUN_HEARTBEAT_INITIAL_SEC"] = "0"
    expected_env.setdefault("LONGRUN_MAX_ACTIVE_JOBS", "4")
    expected_tools = expected_server.setdefault("tools", {})
    for tool, approval in TOOL_APPROVALS.items():
        expected_tools.setdefault(tool, {"approval_mode": approval})
    if updated_parsed != expected:
        raise SystemExit("refusing update because fields outside the documented longrun upgrade would change")

    if updated == original:
        print("Already upgraded; config unchanged.")
        return
    if args.dry_run:
        print("Dry run; config unchanged.")
        print("Would enable asynchronous longrun job tools and preserve unrelated settings.")
        return

    backup_path = _write_config(config_path, updated)
    print(f"Updated: {config_path}")
    print(f"Backup: {backup_path}")
    print("Start a new Codex process before using the upgraded MCP tools.")


if __name__ == "__main__":
    main()
