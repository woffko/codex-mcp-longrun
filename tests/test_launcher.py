from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from codex_mcp_longrun.launcher import LauncherOptions, _parse_args


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


if __name__ == "__main__":
    unittest.main()
