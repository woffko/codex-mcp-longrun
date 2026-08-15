from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parents[1]
TEST_STATE = Path(tempfile.mkdtemp(prefix="codex-longrun-tests-"))
os.environ["LONGRUN_ALLOWED_ROOTS"] = str(TEST_ROOT)
os.environ["LONGRUN_STATE_DIR"] = str(TEST_STATE)
os.environ["LONGRUN_MAX_LOG_BYTES"] = str(1024 * 1024)
os.environ["LONGRUN_MAX_TIMEOUT_SEC"] = "60"
os.environ["LONGRUN_ALLOW_SHELL"] = "0"
os.environ["LONGRUN_HEARTBEAT_INITIAL_SEC"] = "1"
os.environ["LONGRUN_HEARTBEAT_INTERVAL_SEC"] = "2"
os.environ["OPENAI_API_KEY"] = "must-not-reach-child"

from codex_mcp_longrun import server  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402


def _pid_is_gone(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        text = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    fields = text[text.rfind(")") + 2 :].split()
    return not fields or fields[0] == "Z"


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition did not become true before timeout")


class LongrunTests(unittest.IsolatedAsyncioTestCase):
    async def test_01_stdio_handshake_and_health(self) -> None:
        params = StdioServerParameters(
            command=str(TEST_ROOT / ".venv" / "bin" / "codex-mcp-longrun"),
            env=dict(os.environ),
            cwd=TEST_ROOT,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await asyncio.wait_for(session.initialize(), 5)
                tools = await asyncio.wait_for(session.list_tools(), 5)
                health = await asyncio.wait_for(session.call_tool("health", {}), 5)

        self.assertEqual(initialized.server_info.name, "codex-longrun")
        self.assertEqual(
            [tool.name for tool in tools.tools],
            ["health", "start_job", "get_job", "cancel_job", "run_and_wait", "read_log_tail"],
        )
        run_tool = next(tool for tool in tools.tools if tool.name == "run_and_wait")
        self.assertNotIn("ctx", run_tool.input_schema.get("properties", {}))
        self.assertFalse(health.is_error)
        self.assertTrue(health.structured_content["ok"])
        self.assertEqual(health.structured_content["heartbeat_initial_sec"], 1)
        self.assertEqual(health.structured_content["heartbeat_interval_sec"], 2)
        self.assertEqual(health.structured_content["max_active_jobs"], 4)

    async def test_01a_async_job_returns_promptly_and_finishes_without_status_calls(self) -> None:
        params = StdioServerParameters(
            command=str(TEST_ROOT / ".venv" / "bin" / "codex-mcp-longrun"),
            env=dict(os.environ),
            cwd=TEST_ROOT,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                started = time.monotonic()
                submitted = await session.call_tool(
                    "start_job",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "import time; time.sleep(1.2); print('async-final-marker')",
                        ],
                        "cwd": str(TEST_ROOT),
                        "timeout_sec": 5,
                    },
                    read_timeout_seconds=5,
                )
                submit_duration = time.monotonic() - started
                self.assertLess(submit_duration, 2.0)
                self.assertFalse(submitted.is_error)
                job_id = submitted.structured_content["job_id"]
                self.assertRegex(job_id, r"^[a-f0-9]{32}$")
                self.assertEqual(submitted.structured_content["state"], "running")
                self.assertFalse(submitted.structured_content["automatic_wakeup"])

                # The client deliberately makes no MCP status request while the command runs.
                metadata_path = Path(submitted.structured_content["metadata_path"])
                await _wait_for(
                    lambda: json.loads(metadata_path.read_text(encoding="utf-8")).get("state")
                    in server.TERMINAL_JOB_STATES,
                    timeout=5,
                )
                completed = await session.call_tool("get_job", {"job_id": job_id}, read_timeout_seconds=5)

        self.assertFalse(completed.is_error)
        self.assertEqual(completed.structured_content["state"], "succeeded")
        self.assertTrue(completed.structured_content["terminal"])
        self.assertIn("async-final-marker", completed.structured_content["tail"])

    async def test_01aa_second_server_does_not_recover_live_async_job_and_can_cancel_it(self) -> None:
        pid_file = TEST_STATE / "cross-session-async.pid"
        code = (
            "import os,pathlib,sys,time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(60)"
        )
        params = StdioServerParameters(
            command=str(TEST_ROOT / ".venv" / "bin" / "codex-mcp-longrun"),
            env=dict(os.environ),
            cwd=TEST_ROOT,
        )
        async with stdio_client(params) as (first_read, first_write):
            async with ClientSession(first_read, first_write) as first:
                await first.initialize()
                submitted = await first.call_tool(
                    "start_job",
                    {
                        "argv": [sys.executable, "-c", code, str(pid_file)],
                        "cwd": str(TEST_ROOT),
                        "timeout_sec": 60,
                        "grace_period_sec": 1,
                    },
                    read_timeout_seconds=5,
                )
                job_id = submitted.structured_content["job_id"]
                await _wait_for(pid_file.is_file)
                child_pid = int(pid_file.read_text(encoding="utf-8"))

                async with stdio_client(params) as (second_read, second_write):
                    async with ClientSession(second_read, second_write) as second:
                        await second.initialize()
                        live = await second.call_tool("get_job", {"job_id": job_id}, read_timeout_seconds=5)
                        self.assertEqual(live.structured_content["state"], "running")
                        cancelled = await second.call_tool(
                            "cancel_job", {"job_id": job_id}, read_timeout_seconds=5
                        )
                        self.assertTrue(cancelled.structured_content["cancellation_requested"])
                        await _wait_for(lambda: _pid_is_gone(child_pid), timeout=8)
                        await asyncio.sleep(0.2)
                        final = await second.call_tool("get_job", {"job_id": job_id}, read_timeout_seconds=5)

        self.assertEqual(final.structured_content["state"], "cancelled")
        self.assertTrue(final.structured_content["terminal"])

    async def test_01ab_global_active_job_limit_is_locked(self) -> None:
        fake_paths: list[Path] = []
        try:
            for index in range(server.MAX_ACTIVE_JOBS):
                job_id = f"{index + 1:032x}"
                path = TEST_STATE / "jobs" / f"{job_id}.json"
                server._atomic_write_json(
                    path,
                    {
                        "job_id": job_id,
                        "state": "running",
                        "server_pid": os.getpid(),
                        "server_start_ticks": server._proc_start_ticks(os.getpid()),
                        "started_at_utc": server._utc_now(),
                        "cwd": str(TEST_ROOT),
                    },
                )
                fake_paths.append(path)
            with self.assertRaisesRegex(RuntimeError, "active job limit reached"):
                server._reserve_job("f" * 32, str(TEST_ROOT))
        finally:
            for path in fake_paths:
                path.unlink(missing_ok=True)

    async def test_01b_progress_heartbeats(self) -> None:
        progress_events: list[tuple[float, float | None, str | None]] = []

        async def record_progress(progress: float, total: float | None, message: str | None) -> None:
            progress_events.append((progress, total, message))

        params = StdioServerParameters(
            command=str(TEST_ROOT / ".venv" / "bin" / "codex-mcp-longrun"),
            env=dict(os.environ),
            cwd=TEST_ROOT,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "run_and_wait",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "import time; print('private-log-marker', flush=True); time.sleep(3.3)",
                        ],
                        "cwd": str(TEST_ROOT),
                        "timeout_sec": 8,
                    },
                    read_timeout_seconds=10,
                    progress_callback=record_progress,
                )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["state"], "succeeded")
        self.assertGreaterEqual(len(progress_events), 2)
        self.assertTrue(all(total is None for _, total, _ in progress_events))
        self.assertTrue(all(message and message.startswith("Still running:") for _, _, message in progress_events))
        self.assertTrue(all("private-log-marker" not in (message or "") for _, _, message in progress_events))
        metadata = json.loads(Path(result.structured_content["metadata_path"]).read_text(encoding="utf-8"))
        self.assertGreaterEqual(metadata["heartbeat_reports_completed"], 2)
        self.assertIsNone(metadata["heartbeat_error"])

    async def test_01c_progress_failure_does_not_fail_command(self) -> None:
        class FailingContext:
            calls = 0

            async def report_progress(
                self,
                progress: float,
                total: float | None = None,
                message: str | None = None,
            ) -> None:
                self.calls += 1
                raise RuntimeError("client rejected progress")

        context = FailingContext()
        result = await server.run_and_wait(
            argv=[sys.executable, "-c", "import time; time.sleep(1.3)"],
            cwd=str(TEST_ROOT),
            timeout_sec=5,
            ctx=context,  # type: ignore[arg-type]
        )

        self.assertEqual(result.state, "succeeded")
        self.assertEqual(context.calls, 1)
        metadata = json.loads(Path(result.metadata_path).read_text(encoding="utf-8"))
        self.assertEqual(metadata["heartbeat_reports_completed"], 0)
        self.assertEqual(metadata["heartbeat_error"], "RuntimeError")

    async def test_02_success_tail_and_environment_filter(self) -> None:
        code = (
            "import os; "
            "print('secret=' + os.environ.get('OPENAI_API_KEY', '<missing>')); "
            "print('final-marker')"
        )
        result = await server.run_and_wait(
            argv=[sys.executable, "-c", code],
            cwd=str(TEST_ROOT),
            timeout_sec=5,
            success_contains="final-marker",
            tail_bytes=4096,
        )
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("secret=<missing>", result.tail)
        self.assertNotIn("must-not-reach-child", result.tail)
        tail = await server.read_log_tail(result.job_id, tail_bytes=4096)
        self.assertIn("final-marker", tail.tail)

        metadata_path = Path(result.metadata_path)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["cwd"] = "/tmp"
        server._atomic_write_json(metadata_path, metadata)
        with self.assertRaisesRegex(PermissionError, "outside the currently allowed roots"):
            await server.get_job(result.job_id)
        with self.assertRaisesRegex(PermissionError, "outside the currently allowed roots"):
            await server.read_log_tail(result.job_id, tail_bytes=4096)
        metadata["cwd"] = str(TEST_ROOT)
        server._atomic_write_json(metadata_path, metadata)

    async def test_03_rejections_and_nonzero_exit(self) -> None:
        shell_link = TEST_STATE / "innocent-name"
        shell_link.symlink_to("/bin/sh")
        shell = await server.run_and_wait(
            argv=["sh", "-c", "true"], cwd=str(TEST_ROOT), timeout_sec=5
        )
        linked_shell = await server.run_and_wait(
            argv=[str(shell_link), "-c", "true"], cwd=str(TEST_ROOT), timeout_sec=5
        )
        outside = await server.run_and_wait(
            argv=[sys.executable, "-c", "pass"], cwd="/tmp", timeout_sec=5
        )
        failed = await server.run_and_wait(
            argv=[sys.executable, "-c", "raise SystemExit(7)"],
            cwd=str(TEST_ROOT),
            timeout_sec=5,
        )
        self.assertEqual(shell.state, "spawn_error")
        self.assertIn("shell execution is disabled", shell.error or "")
        self.assertEqual(linked_shell.state, "spawn_error")
        self.assertIn("shell execution is disabled", linked_shell.error or "")
        self.assertEqual(outside.state, "spawn_error")
        self.assertIn("outside LONGRUN_ALLOWED_ROOTS", outside.error or "")
        self.assertEqual(failed.state, "failed")
        self.assertEqual(failed.exit_code, 7)

    async def test_04_hard_and_inactivity_timeouts(self) -> None:
        hard = await server.run_and_wait(
            argv=[sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=str(TEST_ROOT),
            timeout_sec=1,
            grace_period_sec=1,
        )
        inactive = await server.run_and_wait(
            argv=[sys.executable, "-c", "import time; print('start', flush=True); time.sleep(10)"],
            cwd=str(TEST_ROOT),
            timeout_sec=10,
            no_output_timeout_sec=1,
            grace_period_sec=1,
        )
        self.assertEqual(hard.state, "timed_out")
        self.assertEqual(inactive.state, "inactive_timeout")

    async def test_05_log_cap(self) -> None:
        result = await server.run_and_wait(
            argv=[sys.executable, "-c", "import os; os.write(1, b'x' * 1200000)"],
            cwd=str(TEST_ROOT),
            timeout_sec=10,
            tail_bytes=1024,
        )
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(result.output_bytes_seen, 1_200_000)
        self.assertEqual(result.output_bytes_logged, 1024 * 1024)
        self.assertTrue(result.log_truncated)
        self.assertEqual(Path(result.log_path).stat().st_size, 1024 * 1024)

    async def test_06_cancellation_kills_command(self) -> None:
        pid_file = TEST_STATE / "cancel-child.pid"
        code = (
            "import os,pathlib,sys,time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(60)"
        )
        task = asyncio.create_task(
            server.run_and_wait(
                argv=[sys.executable, "-c", code, str(pid_file)],
                cwd=str(TEST_ROOT),
                timeout_sec=60,
                grace_period_sec=1,
            )
        )
        await _wait_for(pid_file.is_file)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await _wait_for(lambda: _pid_is_gone(child_pid))
        cancelled = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (TEST_STATE / "jobs").glob("*.json")
            if json.loads(path.read_text(encoding="utf-8")).get("state") == "cancelled"
        ]
        self.assertTrue(cancelled)

    async def test_06b_mcp_cancellation_is_shielded(self) -> None:
        pid_file = TEST_STATE / "mcp-cancel-child.pid"
        code = (
            "import os,pathlib,sys,time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(60)"
        )
        params = StdioServerParameters(
            command=str(TEST_ROOT / ".venv" / "bin" / "codex-mcp-longrun"),
            env=dict(os.environ),
            cwd=TEST_ROOT,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                call = asyncio.create_task(
                    session.call_tool(
                        "run_and_wait",
                        {
                            "argv": [sys.executable, "-c", code, str(pid_file)],
                            "cwd": str(TEST_ROOT),
                            "timeout_sec": 60,
                            "grace_period_sec": 1,
                        },
                        read_timeout_seconds=65,
                    )
                )
                await _wait_for(pid_file.is_file)
                child_pid = int(pid_file.read_text(encoding="utf-8"))
                call.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await call
                await _wait_for(lambda: _pid_is_gone(child_pid), timeout=8)
                await _wait_for(
                    lambda: any(
                        json.loads(path.read_text(encoding="utf-8")).get("state") == "cancelled"
                        and json.loads(path.read_text(encoding="utf-8")).get("command_pgid") == child_pid
                        for path in (TEST_STATE / "jobs").glob("*.json")
                    ),
                    timeout=8,
                )

    async def test_06c_cancellation_during_supervisor_startup(self) -> None:
        pid_file = TEST_STATE / "startup-cancel-child.pid"
        code = (
            "import os,pathlib,sys,time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(60)"
        )
        original_reader = server._read_command_pgid

        async def delayed_reader(status_read_fd: int) -> int:
            try:
                await asyncio.sleep(60)
            finally:
                os.close(status_read_fd)

        server._read_command_pgid = delayed_reader
        try:
            call = asyncio.create_task(
                server.run_and_wait(
                    argv=[sys.executable, "-c", code, str(pid_file)],
                    cwd=str(TEST_ROOT),
                    timeout_sec=60,
                    grace_period_sec=1,
                )
            )
            await _wait_for(pid_file.is_file)
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call
            await _wait_for(lambda: _pid_is_gone(child_pid), timeout=8)
            await _wait_for(
                lambda: any(
                    json.loads(path.read_text(encoding="utf-8")).get("state") == "cancelled"
                    and json.loads(path.read_text(encoding="utf-8")).get("error")
                    == "cancelled while waiting for supervisor startup"
                    for path in (TEST_STATE / "jobs").glob("*.json")
                ),
                timeout=8,
            )
        finally:
            server._read_command_pgid = original_reader

    async def test_07_descendant_is_not_left_running(self) -> None:
        pid_file = TEST_STATE / "descendant.pid"
        code = """
import pathlib
import signal
import subprocess
import sys
import time

child_code = "import os,pathlib,signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); time.sleep(60)"
subprocess.Popen([sys.executable, "-c", child_code, sys.argv[1]])
time.sleep(60)
"""
        result = await server.run_and_wait(
            argv=[sys.executable, "-c", code, str(pid_file)],
            cwd=str(TEST_ROOT),
            timeout_sec=2,
            grace_period_sec=1,
        )
        self.assertEqual(result.state, "timed_out")
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        await _wait_for(lambda: _pid_is_gone(child_pid))

    async def test_08_parent_death_kills_command_and_recovers_metadata(self) -> None:
        pid_file = TEST_STATE / "parent-death-child.pid"
        helper_env = dict(os.environ)
        helper_env["TEST_ROOT"] = str(TEST_ROOT)
        helper_env["TEST_CHILD_PID_FILE"] = str(pid_file)
        helper = await asyncio.create_subprocess_exec(
            sys.executable,
            str(TEST_ROOT / "tests" / "helpers" / "run_until_killed.py"),
            cwd=str(TEST_ROOT),
            env=helper_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            await _wait_for(pid_file.is_file)
            await _wait_for(
                lambda: any(
                    json.loads(path.read_text(encoding="utf-8")).get("state") == "running"
                    for path in (TEST_STATE / "jobs").glob("*.json")
                )
            )
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            helper.send_signal(signal.SIGKILL)
            await helper.wait()
            await _wait_for(lambda: _pid_is_gone(child_pid), timeout=8)

            recovery = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "from codex_mcp_longrun import server; print(server.RECOVERED_ORPHAN_JOBS)",
                cwd=str(TEST_ROOT),
                env=helper_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(recovery.communicate(), 5)
            self.assertEqual(recovery.returncode, 0, stderr.decode("utf-8", errors="replace"))
            self.assertGreaterEqual(int(stdout.decode("utf-8").strip()), 1)
            states = {
                json.loads(path.read_text(encoding="utf-8")).get("state")
                for path in (TEST_STATE / "jobs").glob("*.json")
            }
            self.assertIn("orphaned_recovered", states)
        finally:
            if helper.returncode is None:
                helper.kill()
                await helper.wait()

    async def test_09_recovery_kills_stopped_supervisor_and_command_group(self) -> None:
        pid_file = TEST_STATE / "stopped-supervisor-child.pid"
        helper_env = dict(os.environ)
        helper_env["TEST_ROOT"] = str(TEST_ROOT)
        helper_env["TEST_CHILD_PID_FILE"] = str(pid_file)
        existing_metadata = set((TEST_STATE / "jobs").glob("*.json"))
        helper = await asyncio.create_subprocess_exec(
            sys.executable,
            str(TEST_ROOT / "tests" / "helpers" / "run_until_killed.py"),
            cwd=str(TEST_ROOT),
            env=helper_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        supervisor_pid: int | None = None
        try:
            await _wait_for(pid_file.is_file)

            def find_running_metadata() -> Path | None:
                for path in set((TEST_STATE / "jobs").glob("*.json")) - existing_metadata:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if payload.get("state") == "running" and payload.get("command_pgid"):
                        return path
                return None

            await _wait_for(lambda: find_running_metadata() is not None)
            metadata_path = find_running_metadata()
            assert metadata_path is not None
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            supervisor_pid = int(payload["pid"])
            command_pid = int(pid_file.read_text(encoding="utf-8"))

            os.kill(supervisor_pid, signal.SIGSTOP)
            helper.kill()
            await helper.wait()
            self.assertFalse(_pid_is_gone(command_pid))

            recovery = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "from codex_mcp_longrun import server; print(server.RECOVERED_ORPHAN_JOBS)",
                cwd=str(TEST_ROOT),
                env=helper_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(recovery.communicate(), 5)
            self.assertEqual(recovery.returncode, 0, stderr.decode("utf-8", errors="replace"))
            self.assertGreaterEqual(int(stdout.decode("utf-8").strip()), 1)
            await _wait_for(lambda: _pid_is_gone(command_pid), timeout=8)
            await _wait_for(lambda: _pid_is_gone(supervisor_pid), timeout=8)

            recovered = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(recovered["state"], "orphaned_recovered")
            self.assertTrue(recovered["recovery_terminated_command_group"])
            self.assertTrue(recovered["recovery_terminated_supervisor"])
        finally:
            if helper.returncode is None:
                helper.kill()
                await helper.wait()
            if supervisor_pid is not None and not _pid_is_gone(supervisor_pid):
                os.kill(supervisor_pid, signal.SIGKILL)


def tearDownModule() -> None:
    shutil.rmtree(TEST_STATE, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
