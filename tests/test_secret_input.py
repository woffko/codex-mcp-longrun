from __future__ import annotations

import os
import contextlib
import io
import stat
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from codex_mcp_longrun.secret_input import (
    claim_one_time_secret,
    create_one_time_secret,
    secret_dir,
    main,
)


class SecretInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="longrun-secret-tests-")
        self.state_dir = Path(self.temporary.name) / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_secret_is_private_single_use_and_unlinked_on_claim(self) -> None:
        secret_id = create_one_time_secret(self.state_dir, b"fixture-secret\n")
        path = secret_dir(self.state_dir) / f"{secret_id}.stdin"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        descriptor = claim_one_time_secret(self.state_dir, secret_id)
        try:
            self.assertFalse(path.exists())
            self.assertEqual(os.read(descriptor, 1024), b"fixture-secret\n")
        finally:
            os.close(descriptor)
        with self.assertRaises(ValueError):
            claim_one_time_secret(self.state_dir, secret_id)

    def test_expired_wrong_mode_and_symlink_handles_are_rejected(self) -> None:
        expired_id = create_one_time_secret(self.state_dir, b"expired\n")
        expired_path = secret_dir(self.state_dir) / f"{expired_id}.stdin"
        old = time.time() - 600
        os.utime(expired_path, (old, old))
        with self.assertRaisesRegex(ValueError, "expired"):
            claim_one_time_secret(self.state_dir, expired_id, ttl_sec=300)

        mode_id = create_one_time_secret(self.state_dir, b"mode\n")
        mode_path = secret_dir(self.state_dir) / f"{mode_id}.stdin"
        mode_path.chmod(0o640)
        with self.assertRaisesRegex(ValueError, "0600"):
            claim_one_time_secret(self.state_dir, mode_id)

        target = Path(self.temporary.name) / "target"
        target.write_bytes(b"symlink-secret\n")
        link_id = uuid.uuid4().hex
        link_path = secret_dir(self.state_dir) / f"{link_id}.stdin"
        link_path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "safe regular file"):
            claim_one_time_secret(self.state_dir, link_id)

    def test_size_and_handle_validation(self) -> None:
        with self.assertRaises(ValueError):
            create_one_time_secret(self.state_dir, b"")
        with self.assertRaises(ValueError):
            create_one_time_secret(self.state_dir, b"12345", max_bytes=4)
        with self.assertRaises(ValueError):
            claim_one_time_secret(self.state_dir, "not-a-handle")

    def test_cli_prints_only_handle_and_never_secret(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "codex_mcp_longrun.secret_input.getpass.getpass",
            side_effect=["cli-fixture-password", "cli-fixture-password"],
        ), patch(
            "sys.argv",
            [
                "codex-longrun-secret",
                "--state-dir",
                str(self.state_dir),
                "--confirm",
            ],
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            main()
        secret_id = stdout.getvalue().strip()
        self.assertRegex(secret_id, r"^[a-f0-9]{32}$")
        self.assertNotIn("cli-fixture-password", stdout.getvalue())
        self.assertNotIn("cli-fixture-password", stderr.getvalue())
        descriptor = claim_one_time_secret(self.state_dir, secret_id)
        try:
            self.assertEqual(os.read(descriptor, 1024), b"cli-fixture-password\n")
        finally:
            os.close(descriptor)


if __name__ == "__main__":
    unittest.main(verbosity=2)
