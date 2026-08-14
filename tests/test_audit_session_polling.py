from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = TEST_ROOT / "scripts" / "audit-session-polling.py"


class AuditSessionPollingTests(unittest.TestCase):
    def test_counts_only_events_after_boundary_without_exposing_messages(self) -> None:
        records = [
            {
                "timestamp": "2026-08-14T10:00:00Z",
                "type": "response_item",
                "payload": {"type": "function_call", "name": "wait"},
            },
            {
                "timestamp": "2026-08-14T11:00:00Z",
                "type": "response_item",
                "payload": {"type": "function_call", "name": "wait", "arguments": "private-message"},
            },
            {
                "timestamp": "2026-08-14T11:00:01Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "wait"},
            },
            {
                "timestamp": "2026-08-14T11:00:02Z",
                "type": "event_msg",
                "payload": {"type": "token_count"},
            },
            {
                "timestamp": "2026-08-14T11:00:03Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "start_job abc123"},
            },
        ]
        with tempfile.TemporaryDirectory(prefix="codex-longrun-audit-test-") as raw:
            path = Path(raw) / "session.jsonl"
            path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(path),
                    "--since",
                    "2026-08-14T11:00:00Z",
                    "--job-id",
                    "abc123",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["model_wait_calls"], 2)
        self.assertEqual(payload["token_events"], 1)
        self.assertEqual(payload["agent_messages"], 1)
        self.assertEqual(payload["start_job_mentions"], 1)
        self.assertEqual(payload["job_id_mentions"], 1)
        self.assertNotIn("private-message", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
