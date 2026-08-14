from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = TEST_ROOT / "scripts" / "enroll-project.py"


def _config_text(initial_root: Path) -> str:
    return f'''model = "test-model"

[unrelated]
marker = "must-stay-unchanged"

[mcp_servers.longrun]
command = "/opt/codex-mcp-longrun"
enabled = true

[mcp_servers.longrun.env]
LONGRUN_STATE_DIR = "/tmp/codex-longrun-state"
LONGRUN_ALLOWED_ROOTS = "{initial_root}" # preserve this policy comment
LONGRUN_ALLOW_SHELL = "0"

[mcp_servers.longrun.tools.health]
approval_mode = "auto"
'''


class EnrollProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-longrun-enroll-tests-")
        self.root = Path(self.temporary.name)
        self.initial_root = self.root / "initial"
        self.new_root = self.root / "new-project"
        self.initial_root.mkdir()
        self.new_root.mkdir()
        self.config_dir = self.root / ".codex"
        self.config_dir.mkdir()
        self.config = self.config_dir / "config.toml"
        self.original = _config_text(self.initial_root)
        self.config.write_text(self.original, encoding="utf-8")
        self.config.chmod(0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, *extra_args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(self.config),
                "--allowed-root",
                str(self.new_root),
                *extra_args,
            ],
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

    def test_enroll_is_atomic_private_and_idempotent(self) -> None:
        first = self._run()
        self.assertEqual(first.returncode, 0, first.stderr)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        roots = parsed["mcp_servers"]["longrun"]["env"]["LONGRUN_ALLOWED_ROOTS"].split(os.pathsep)
        self.assertEqual(roots, [str(self.initial_root), str(self.new_root)])
        self.assertEqual(parsed["unrelated"]["marker"], "must-stay-unchanged")
        self.assertIn("# preserve this policy comment", self.config.read_text(encoding="utf-8"))
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

        backups = list((self.config_dir / "backups").glob("config.toml.before-longrun-enroll-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), self.original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

        updated = self.config.read_text(encoding="utf-8")
        second = self._run()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Already enrolled", second.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), updated)
        self.assertEqual(
            len(list((self.config_dir / "backups").glob("config.toml.before-longrun-enroll-*"))),
            1,
        )

    def test_refuses_filesystem_root(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--config",
                str(self.config),
                "--allowed-root",
                "/",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing broad allowed root", result.stderr)
        self.assertEqual(self.config.read_text(encoding="utf-8"), self.original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
