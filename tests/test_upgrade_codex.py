from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = TEST_ROOT / "scripts" / "upgrade-codex.py"


def _config_text(heartbeat: str | None = None) -> str:
    heartbeat_line = "" if heartbeat is None else f'LONGRUN_HEARTBEAT_INITIAL_SEC = "{heartbeat}"\n'
    return f'''model = "test-model"

[unrelated]
marker = "must-stay-unchanged"

[mcp_servers.longrun]
command = "/opt/codex-mcp-longrun"
enabled = true
enabled_tools = ["health", "run_and_wait", "read_log_tail"] # replace only this value

[mcp_servers.longrun.env]
LONGRUN_STATE_DIR = "/tmp/codex-longrun-state"
LONGRUN_ALLOWED_ROOTS = "/tmp/project"
{heartbeat_line}LONGRUN_ALLOW_SHELL = "0" # preserve this policy comment

[mcp_servers.longrun.tools.health]
approval_mode = "auto"

[mcp_servers.longrun.tools.run_and_wait]
approval_mode = "prompt"
'''


class UpgradeCodexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-longrun-upgrade-tests-")
        self.root = Path(self.temporary.name)
        self.config_dir = self.root / ".codex"
        self.config_dir.mkdir()
        self.config = self.config_dir / "config.toml"
        self.original = _config_text()
        self.config.write_text(self.original, encoding="utf-8")
        self.config.chmod(0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, *extra_args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(self.config), *extra_args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_dry_run_does_not_modify_config(self) -> None:
        result = self._run("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run", result.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), self.original)
        self.assertFalse((self.config_dir / "backups").exists())

    def test_upgrade_is_private_targeted_and_idempotent(self) -> None:
        first = self._run()
        self.assertEqual(first.returncode, 0, first.stderr)
        updated_text = self.config.read_text(encoding="utf-8")
        parsed = tomllib.loads(updated_text)
        longrun = parsed["mcp_servers"]["longrun"]
        self.assertEqual(
            longrun["enabled_tools"],
            ["health", "start_job", "get_job", "cancel_job", "run_and_wait", "read_log_tail"],
        )
        self.assertEqual(longrun["env_vars"], ["LONGRUN_BRIDGE_SOCKET"])
        self.assertEqual(longrun["env"]["LONGRUN_HEARTBEAT_INITIAL_SEC"], "0")
        self.assertEqual(longrun["env"]["LONGRUN_MAX_ACTIVE_JOBS"], "4")
        self.assertEqual(longrun["env"]["LONGRUN_SECRET_TTL_SEC"], "300")
        self.assertEqual(longrun["env"]["LONGRUN_MAX_STDIN_SECRET_BYTES"], "65536")
        self.assertEqual(longrun["tools"]["start_job"]["approval_mode"], "prompt")
        self.assertEqual(longrun["tools"]["get_job"]["approval_mode"], "auto")
        self.assertEqual(longrun["tools"]["cancel_job"]["approval_mode"], "prompt")
        self.assertEqual(longrun["tools"]["run_and_wait"]["approval_mode"], "prompt")
        self.assertEqual(longrun["tools"]["read_log_tail"]["approval_mode"], "prompt")
        self.assertEqual(parsed["unrelated"]["marker"], "must-stay-unchanged")
        self.assertIn("# preserve this policy comment", updated_text)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

        backups = list((self.config_dir / "backups").glob("config.toml.before-longrun-upgrade-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), self.original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

        second = self._run()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Already upgraded", second.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), updated_text)
        self.assertEqual(
            len(list((self.config_dir / "backups").glob("config.toml.before-longrun-upgrade-*"))),
            1,
        )

    def test_preserves_explicit_heartbeat_unless_reset(self) -> None:
        self.config.write_text(_config_text("300"), encoding="utf-8")
        preserved = self._run()
        self.assertEqual(preserved.returncode, 0, preserved.stderr)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(parsed["mcp_servers"]["longrun"]["env"]["LONGRUN_HEARTBEAT_INITIAL_SEC"], "300")

        reset = self._run("--reset-heartbeat")
        self.assertEqual(reset.returncode, 0, reset.stderr)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(parsed["mcp_servers"]["longrun"]["env"]["LONGRUN_HEARTBEAT_INITIAL_SEC"], "0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
