#!/usr/bin/env python3
"""Add exact trusted project roots to an existing longrun MCP configuration."""

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


SETTING_NAME = "LONGRUN_ALLOWED_ROOTS"


def _resolve_project_root(raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise SystemExit(f"allowed root must be absolute: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SystemExit(f"allowed root does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise SystemExit(f"allowed root is not a directory: {resolved}")
    if resolved in {Path("/"), Path.home().resolve()}:
        raise SystemExit(f"refusing broad allowed root: {resolved}")
    return resolved


def _configured_roots(parsed: dict[str, object], server_name: str) -> list[Path]:
    try:
        mcp_servers = parsed["mcp_servers"]
        server = mcp_servers[server_name]  # type: ignore[index]
        env = server["env"]  # type: ignore[index]
        raw_roots = env[SETTING_NAME]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise SystemExit(
            f"mcp_servers.{server_name}.env.{SETTING_NAME} is missing; "
            "configure the server before enrolling another project"
        ) from exc
    if not isinstance(raw_roots, str):
        raise SystemExit(f"mcp_servers.{server_name}.env.{SETTING_NAME} must be a string")

    roots: list[Path] = []
    for raw_root in raw_roots.split(os.pathsep):
        if not raw_root:
            continue
        resolved = _resolve_project_root(raw_root)
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise SystemExit(f"mcp_servers.{server_name}.env.{SETTING_NAME} is empty")
    return roots


def _replace_setting(original: str, server_name: str, value: str) -> str:
    lines = original.splitlines(keepends=True)
    section_pattern = re.compile(rf"^\s*\[mcp_servers\.{re.escape(server_name)}\.env\]\s*(?:#.*)?$")
    section_indexes = [
        index for index, line in enumerate(lines) if section_pattern.fullmatch(line.rstrip("\r\n"))
    ]
    if len(section_indexes) != 1:
        raise SystemExit(
            f"expected exactly one [mcp_servers.{server_name}.env] section, found {len(section_indexes)}"
        )

    section_start = section_indexes[0] + 1
    section_end = len(lines)
    for index in range(section_start, len(lines)):
        if lines[index].lstrip().startswith("["):
            section_end = index
            break

    assignment_pattern = re.compile(
        rf'''^(\s*){SETTING_NAME}\s*=\s*(?:"(?:\\.|[^"\\])*"|'[^']*')(\s*(?:#.*)?)$'''
    )
    assignment_indexes = [
        index
        for index in range(section_start, section_end)
        if assignment_pattern.fullmatch(lines[index].rstrip("\r\n"))
    ]
    if len(assignment_indexes) != 1:
        raise SystemExit(
            f"expected exactly one {SETTING_NAME} assignment in "
            f"[mcp_servers.{server_name}.env], found {len(assignment_indexes)}"
        )

    index = assignment_indexes[0]
    match = assignment_pattern.fullmatch(lines[index].rstrip("\r\n"))
    assert match is not None
    newline = "\r\n" if lines[index].endswith("\r\n") else "\n"
    if not lines[index].endswith(("\n", "\r")):
        newline = ""
    lines[index] = (
        f"{match.group(1)}{SETTING_NAME} = {json.dumps(value, ensure_ascii=False)}"
        f"{match.group(2)}{newline}"
    )
    return "".join(lines)


def _write_config(config_path: Path, original: str, updated: str) -> Path:
    backup_dir = config_path.parent / "backups"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup_dir.chmod(0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"{config_path.name}.before-longrun-enroll-{timestamp}"
    shutil.copy2(config_path, backup_path)
    backup_path.chmod(0o600)

    fd, temporary_name = tempfile.mkstemp(prefix=f".{config_path.name}.longrun-enroll-", dir=config_path.parent)
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
    parser = argparse.ArgumentParser(
        description="Enroll exact trusted project roots in an existing Codex longrun MCP configuration."
    )
    parser.add_argument("--config", default=str(Path.home() / ".codex" / "config.toml"))
    parser.add_argument("--server-name", default="longrun")
    parser.add_argument("--allowed-root", action="append", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve(strict=True)
    if not config_path.is_file():
        raise SystemExit(f"config is not a regular file: {config_path}")

    original = config_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(original)
    current_roots = _configured_roots(parsed, args.server_name)
    requested_roots = [_resolve_project_root(raw) for raw in args.allowed_root]
    updated_roots = list(current_roots)
    for root in requested_roots:
        if root not in updated_roots:
            updated_roots.append(root)

    if updated_roots == current_roots:
        print("Already enrolled; config unchanged.")
        for root in current_roots:
            print(f"  {root}")
        return

    joined_roots = os.pathsep.join(str(root) for root in updated_roots)
    updated = _replace_setting(original, args.server_name, joined_roots)
    updated_parsed = tomllib.loads(updated)
    expected = copy.deepcopy(parsed)
    expected["mcp_servers"][args.server_name]["env"][SETTING_NAME] = joined_roots
    if updated_parsed != expected:
        raise SystemExit("refusing update because fields other than LONGRUN_ALLOWED_ROOTS would change")

    if args.dry_run:
        print("Dry run; config unchanged. Resulting allowed roots:")
        for root in updated_roots:
            print(f"  {root}")
        return

    backup_path = _write_config(config_path, original, updated)
    print(f"Updated: {config_path}")
    print(f"Backup: {backup_path}")
    print("Enrolled roots:")
    for root in updated_roots:
        print(f"  {root}")
    print("Start a new Codex process before using the updated MCP configuration.")


if __name__ == "__main__":
    main()
