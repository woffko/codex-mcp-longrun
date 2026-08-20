from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_mcp_longrun.launcher import LauncherOptions, _parse_args, _resolve_codex_cwd


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


if __name__ == "__main__":
    unittest.main()
