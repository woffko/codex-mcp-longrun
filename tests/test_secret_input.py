from __future__ import annotations

import os
import base64
import fcntl
import json
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
    claim_secret_pair,
    create_one_time_secret,
    secret_dir,
    main,
    validate_secret_pair,
    F_ADD_SEALS,
    F_GET_SEALS,
    SECRET_SEALS,
)


class SecretInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="longrun-secret-tests-")
        self.state_dir = Path(self.temporary.name) / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def pair(self, first: bytes = b"first\x00\xff\n", second: bytes = b"second\n") -> dict[str, str]:
        return {"ssh": create_one_time_secret(self.state_dir, first),
                "gui": create_one_time_secret(self.state_dir, second)}

    def test_pair_is_byte_exact_sealed_single_use_without_disk_bundle(self) -> None:
        handles = self.pair()
        descriptor = claim_secret_pair(self.state_dir, handles)
        try:
            self.assertFalse(os.get_inheritable(descriptor))
            self.assertEqual(fcntl.fcntl(descriptor, F_GET_SEALS) & SECRET_SEALS, SECRET_SEALS)
            payload = json.loads(os.read(descriptor, 65536))
            self.assertEqual(set(payload), {"schemaVersion", "encoding", "secrets"})
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertEqual(payload["encoding"], "base64")
            self.assertEqual({name: base64.b64decode(value, validate=True)
                              for name, value in payload["secrets"].items()},
                             {"ssh": b"first\x00\xff\n", "gui": b"second\n"})
            self.assertEqual(list(secret_dir(self.state_dir).iterdir()), [])
            for operation in (lambda: os.write(descriptor, b"x"),
                              lambda: os.ftruncate(descriptor, 0),
                              lambda: os.ftruncate(descriptor, 100000),
                              lambda: fcntl.fcntl(descriptor, F_ADD_SEALS, SECRET_SEALS)):
                with self.assertRaises(PermissionError):
                    operation()
        finally:
            os.close(descriptor)
        with self.assertRaises(ValueError):
            claim_secret_pair(self.state_dir, handles)

    def test_invalid_pair_never_consumes_valid_handle(self) -> None:
        handles = self.pair()
        first, second = handles.values()
        for invalid in ({}, {"ssh": first}, {"ssh": first, "gui": first},
                        {"ssh": first, "gui": second, "third": "a" * 32},
                        {"unsafe\nrole": first, "gui": second},
                        {"a" * 33: first, "gui": second},
                        {"ssh": first, "gui": "not-a-handle"}):
            with self.assertRaises(ValueError):
                claim_secret_pair(self.state_dir, invalid)
        self.assertIsNone(validate_secret_pair(None))
        self.assertTrue(all((secret_dir(self.state_dir) / f"{handle}.stdin").exists()
                            for handle in handles.values()))

    def test_partial_claim_failure_is_consumed_and_fd_safe(self) -> None:
        handles = self.pair()
        second = secret_dir(self.state_dir) / f"{handles['gui']}.stdin"
        second.chmod(0o640)
        before = len(os.listdir("/proc/self/fd"))
        with self.assertRaisesRegex(ValueError, "could not be assembled"):
            claim_secret_pair(self.state_dir, handles)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        self.assertFalse((secret_dir(self.state_dir) / f"{handles['ssh']}.stdin").exists())
        self.assertTrue(second.exists())

    def test_pair_encoded_limit_failure_closes_memfd(self) -> None:
        handles = self.pair(b"a" * 25000, b"b" * 25000)
        before = len(os.listdir("/proc/self/fd"))
        with self.assertRaises(ValueError):
            claim_secret_pair(self.state_dir, handles)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        self.assertEqual(list(secret_dir(self.state_dir).iterdir()), [])

    def test_pair_read_seal_and_write_failures_are_sanitized_and_fd_safe(self) -> None:
        for target in ("os.read", "os.write", "fcntl.fcntl"):
            handles = self.pair()
            before = len(os.listdir("/proc/self/fd"))
            with patch("codex_mcp_longrun.secret_input." + target,
                       side_effect=OSError("must-not-escape-secret-value")):
                with self.assertRaises(ValueError) as raised:
                    claim_secret_pair(self.state_dir, handles)
            self.assertNotIn("must-not-escape", str(raised.exception))
            self.assertEqual(len(os.listdir("/proc/self/fd")), before)

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
