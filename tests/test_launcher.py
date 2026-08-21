from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from codex_mcp_longrun.launcher import (
    LauncherOptions,
    _guarded_exec_command,
    _parse_args,
    _resolve_codex_cwd,
)


ROOT = Path(__file__).resolve().parents[1]
SPAWN_GUARDED_CHILD = ROOT / "tests" / "helpers" / "spawn_guarded_child.py"


def _process_state(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return stat[stat.rfind(")") + 2 :].split(maxsplit=1)[0]


def _descendant_pids(root_pid: int) -> set[int]:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            fields = stat[stat.rfind(")") + 2 :].split()
            parents[int(entry.name)] = int(fields[1])
        except (FileNotFoundError, IndexError, PermissionError, ValueError):
            continue
    descendants: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {pid for pid, parent in parents.items() if parent in frontier}
        children -= descendants
        descendants.update(children)
        frontier = children
    return descendants


def _wait_for(predicate: Callable[[], bool], timeout_sec: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class LauncherArgumentTests(unittest.TestCase):
    def test_proxy_options_are_consumed_and_codex_arguments_are_preserved(self) -> None:
        argv = [
            "codex-longrun",
            "--longrun-legacy-history",
            "omit",
            "--longrun-history-threshold-mib",
            "32",
            "--longrun-history-tail-turns",
            "3",
            "--longrun-history-timeout-sec",
            "90",
            "resume",
            "-C",
            "/project",
            "session-id",
        ]
        with patch.object(sys, "argv", argv):
            remaining, options = _parse_args()

        self.assertEqual(remaining, ["resume", "-C", "/project", "session-id"])
        self.assertEqual(
            options,
            LauncherOptions(
                legacy_history_mode="omit",
                history_threshold_mib=32,
                history_tail_turns=3,
                helper_timeout_sec=90,
            ),
        )

    def test_default_proxy_mode_is_auto(self) -> None:
        with patch.object(sys, "argv", ["codex-longrun", "-C", "/project"]):
            remaining, options = _parse_args()

        self.assertEqual(remaining, ["-C", "/project"])
        self.assertEqual(options, LauncherOptions())

    def test_guarded_exec_command_uses_current_launcher_pid(self) -> None:
        command = _guarded_exec_command("tool", "argument")

        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:4], ("-m", "codex_mcp_longrun.parent_guard", "--parent-pid"))
        self.assertEqual(command[4], str(os.getpid()))
        self.assertEqual(command[5:], ("--exec-only", "--", "tool", "argument"))

    def test_codex_cwd_is_resolved_before_app_server_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            project.mkdir()

            resolved = _resolve_codex_cwd(
                ["resume", "-C", "project", "session-id"], base
            )

            self.assertEqual(resolved, project.resolve())

    def test_codex_long_cd_form_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()

            resolved = _resolve_codex_cwd([f"--cd={project}"], Path(directory))

            self.assertEqual(resolved, project.resolve())

    def test_codex_cwd_defaults_to_launcher_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)

            self.assertEqual(_resolve_codex_cwd(["resume", "session-id"], base), base)

    def test_codex_cwd_does_not_parse_arguments_after_separator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)

            resolved = _resolve_codex_cwd(["--", "-C", "/missing"], base)

            self.assertEqual(resolved, base)

    def test_codex_cwd_rejects_missing_value(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires a working-directory"):
            _resolve_codex_cwd(["resume", "-C"])

    def test_codex_cwd_rejects_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"

            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                _resolve_codex_cwd(["--cd", str(missing)], Path(directory))


@unittest.skipUnless(sys.platform == "linux", "parent-death guard requires Linux")
class ParentDeathGuardTests(unittest.TestCase):
    def test_guard_executes_command_for_live_parent(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "codex_mcp_longrun.parent_guard",
                "--parent-pid",
                str(os.getpid()),
                "--exec-only",
                "--",
                sys.executable,
                "-c",
                "print('guarded')",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "guarded")

    def test_guard_closes_parent_exit_before_arm_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "target-started"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "codex_mcp_longrun.parent_guard",
                    "--parent-pid",
                    str(os.getpid() + 1_000_000),
                    "--exec-only",
                    "--",
                    sys.executable,
                    "-c",
                    "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()",
                    str(marker),
                ],
                cwd=ROOT,
                check=False,
            )

            self.assertEqual(result.returncode, 128 + signal.SIGTERM)
            self.assertFalse(marker.exists())

    def test_official_app_server_dies_after_launcher_sigkill(self) -> None:
        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("codex executable is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "app.sock"
            parent = subprocess.Popen(
                [
                    sys.executable,
                    str(SPAWN_GUARDED_CHILD),
                    codex,
                    "app-server",
                    "--listen",
                    f"unix://{socket_path}",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            guarded_pid = -1
            observed_pids: set[int] = set()
            try:
                assert parent.stdout is not None
                guarded_pid = int(parent.stdout.readline().strip())
                ready = _wait_for(
                    lambda: socket_path.exists() or parent.poll() is not None
                )
                self.assertTrue(ready, "official App Server startup did not settle")
                if not socket_path.exists():
                    assert parent.stderr is not None
                    stderr = parent.stderr.read()
                    if any(
                        marker in stderr
                        for marker in ("Operation not permitted", "Read-only file system")
                    ):
                        self.skipTest(
                            f"sandbox does not permit App Server startup: {stderr.strip()}"
                        )
                    self.fail(f"official App Server did not create its socket: {stderr}")
                observed_pids = {guarded_pid, *_descendant_pids(guarded_pid)}
                self.assertGreaterEqual(len(observed_pids), 2)
                time.sleep(0.2)
                self.assertTrue(
                    all(_process_state(pid) not in {None, "Z"} for pid in observed_pids),
                    "guarded App Server did not remain stable while launcher was alive",
                )

                os.kill(parent.pid, signal.SIGKILL)
                parent.wait(timeout=5)

                self.assertTrue(
                    _wait_for(
                        lambda: all(
                            _process_state(pid) in {None, "Z"} for pid in observed_pids
                        )
                    ),
                    f"guarded App Server processes survived: {observed_pids}",
                )
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                for pid in observed_pids:
                    if _process_state(pid) not in {None, "Z"}:
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(pid, signal.SIGKILL)
                if parent.stdout is not None:
                    parent.stdout.close()
                if parent.stderr is not None:
                    parent.stderr.close()


if __name__ == "__main__":
    unittest.main()
