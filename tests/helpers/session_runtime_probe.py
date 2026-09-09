"""Opt-in live Codex/coordinator test with a local deterministic model, no API key.

Run with the project venv: python tests/helpers/session_runtime_probe.py [--resume]
All processes, configuration, jobs and provider requests use a private scratch root.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.asyncio.client import unix_connect


async def probe(args: argparse.Namespace) -> None:
    root = Path(tempfile.mkdtemp(prefix="longrun-session-live-"))
    root.chmod(0o700)
    source = Path(__file__).resolve().parents[2] / "src"
    requests: list[dict] = []
    events: list[dict] = []
    seed_mode = args.resume
    goal_mode = args.policy == "goal"
    goal_job_id = None
    goal_completed = threading.Event()
    code = "import time,sys; time.sleep(1); print('SESSION_JOB_OK'); sys.exit(" + ("7" if args.outcome == "failure" else "0") + ")"
    if args.outcome == "timeout":
        code = "import time; time.sleep(8)"
    job_args = {"argv": [sys.executable, "-c", code], "cwd": str(root), "grace_period_sec": 1,
                "timeout_sec": 1 if args.outcome == "timeout" else 5, "wake_policy": args.policy}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *unused: object) -> None:
            pass

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            title_mode = any(x.get("role") == "user" and "Do not answer the request." in json.dumps(x)
                             and "User prompt:" in json.dumps(x) for x in request.get("input", []))
            if not title_mode:
                requests.append(request)
            number = len(requests)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            response = {"id": f"resp_{number}", "object": "response", "created_at": int(time.time()),
                        "status": "in_progress", "output": []}
            def send(kind: str, **fields: object) -> None:
                self.wfile.write((f"event: {kind}\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode())
                self.wfile.flush()
            if title_mode:
                item = {"type": "message", "id": "title_item", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "Longrun session test", "annotations": []}]}
            elif seed_mode:
                item = {"type": "message", "id": "seed_item", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "READY", "annotations": []}]}
            elif number == 1 or (args.chain and number == 3):
                code = "text(await tools.mcp__longrun__start_job(" + json.dumps(job_args) + ")); text('HANDOFF_MUST_NOT_RETURN');"
                if goal_mode:
                    code = "text(await tools.mcp__longrun__start_job(" + json.dumps(job_args) + "));"
                item = {"type": "custom_tool_call", "id": f"start_item_{number}", "call_id": f"start_call_{number}",
                        "namespace": "functions", "name": "exec", "input": code}
            elif goal_mode and number == 2:
                time.sleep(args.goal_final_delay)
                item = {"type": "message", "id": "goal_wait_item", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "Waiting for the registered Longrun job.", "annotations": []}]}
            elif goal_mode and number == 3:
                item = {"type": "custom_tool_call", "id": "goal_get_item", "call_id": "goal_get_call",
                        "namespace": "functions", "name": "exec", "input":
                        "text(await tools.mcp__longrun__get_job(" + json.dumps({"job_id": goal_job_id}) + "));"}
            elif goal_mode:
                # The test driver completes its synthetic Goal after observing
                # the result, so no autonomous extra turn can escape this fixture.
                goal_completed.wait(5)
                item = {"type": "message", "id": "goal_done_item", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "GOAL_WAKE_OK", "annotations": []}]}
            elif number == 2 and args.user_activity:
                item = {"type": "message", "id": "manual_item", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "MANUAL_OK", "annotations": []}]}
            elif number == 2 or (args.chain and number == 4):
                matches = re.findall(r"\[Longrun session wake\]\\nJob ([a-f0-9]{32})", json.dumps(request["input"]))
                if not matches:
                    item = {"type": "message", "id": "unexpected", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "UNEXPECTED_MODEL_CONTINUATION", "annotations": []}]}
                else:
                    item = {"type": "custom_tool_call", "id": f"get_item_{number}", "call_id": f"get_call_{number}",
                            "namespace": "functions", "name": "exec", "input":
                            "text(await tools.mcp__longrun__get_job(" + json.dumps({"job_id": matches[-1]}) + "));"}
            else:
                item = {"type": "message", "id": "done_item", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "SESSION_WAKE_OK", "annotations": []}]}
            try:
                send("response.created", response=response)
                send("response.output_item.added", output_index=0, item=item)
                send("response.output_item.done", output_index=0, item=item)
                response.update(status="completed", output=[item], usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20})
                send("response.completed", response=response)
            except (BrokenPipeError, ConnectionResetError):
                pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    codex_home = root / "codex-home"
    codex_home.mkdir()
    app_socket, bridge_socket, tui_socket = (root / name for name in ("app.sock", "bridge.sock", "tui.sock"))
    (codex_home / "config.toml").write_text(f'''
model = "gpt-5.6-sol"
model_provider = "probe"
approval_policy = "on-request"
sandbox_mode = "read-only"
[model_providers.probe]
name = "Local deterministic session test"
base_url = "http://127.0.0.1:{provider.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
[mcp_servers.longrun]
command = {json.dumps(sys.executable)}
args = ["-m", "codex_mcp_longrun.server"]
enabled_tools = ["start_job", "get_job"]
env_vars = ["LONGRUN_BRIDGE_SOCKET"]
[mcp_servers.longrun.env]
PYTHONPATH = {json.dumps(str(source))}
LONGRUN_ALLOWED_ROOTS = {json.dumps(str(root))}
LONGRUN_STATE_DIR = {json.dumps(str(root / 'jobs-state'))}
LONGRUN_BRIDGE_SOCKET = {json.dumps(str(bridge_socket))}
''')
    env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG") if k in os.environ}
    env.update(CODEX_HOME=str(codex_home), PYTHONPATH=str(source))
    if args.installed:
        env.pop("PYTHONPATH", None)
        config = codex_home / "config.toml"
        config.write_text(config.read_text().replace(f'PYTHONPATH = {json.dumps(str(source))}\n', ""))
    if args.launcher:
        config = codex_home / "config.toml"
        config.write_text(config.read_text().replace(f'LONGRUN_BRIDGE_SOCKET = {json.dumps(str(bridge_socket))}\n', ""))
    if args.tui:
        # Trust only this generated fixture root; native tool approval is still
        # exercised through the real TUI below.
        with (codex_home / "config.toml").open("a") as config:
            config.write(f'\n[projects.{json.dumps(str(root))}]\ntrust_level = "trusted"\n')
    processes, logs = [], []
    ws = None
    reader_task = None
    async def launch(argv: list[str], log_name: str, sock: Path) -> None:
        log = (root / log_name).open("w")
        logs.append(log)
        process = await asyncio.create_subprocess_exec(*argv, cwd=root, env=env, stdout=log, stderr=log)
        processes.append(process)
        for _ in range(200):
            if sock.exists():
                return
            if process.returncode is not None:
                raise RuntimeError(f"{log_name} exited: {process.returncode}; see {root}")
            await asyncio.sleep(.01)
        raise RuntimeError(f"socket did not appear: {sock}")
    try:
        if not args.launcher:
            await launch([args.codex, "app-server", "--listen", f"unix://{app_socket}"], "app.log", app_socket)
            await launch([sys.executable, "-m", "codex_mcp_longrun.coordinator",
                          "--app-server-socket", str(app_socket), "--bridge-socket", str(bridge_socket),
                          "--tui-socket", str(tui_socket), "--state-db", str(root / "bridge.db")], "coordinator.log", tui_socket)
        if args.tui:
            import fcntl
            import pty
            import struct
            import termios
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            tui_argv = ([sys.executable, "-m", "codex_mcp_longrun.launcher"] if args.launcher else
                        [args.codex, "--remote", f"unix://{tui_socket}"])
            tui = await asyncio.create_subprocess_exec(*tui_argv,
                "--no-alt-screen", "-C", str(root), stdin=slave, stdout=slave, stderr=slave,
                cwd=root, env={**env, "TERM": "xterm-256color"})
            processes.append(tui)
            os.close(slave)
            os.set_blocking(master, False)
            chunks = asyncio.Queue()
            loop = asyncio.get_running_loop()
            def terminal_read() -> None:
                try:
                    data = os.read(master, 65536)
                except (BlockingIOError, OSError):
                    return
                if data:
                    chunks.put_nowait(data)
            loop.add_reader(master, terminal_read)
            transcript = bytearray()
            sent = False
            approved = False
            try:
                while True:
                    data = await asyncio.wait_for(chunks.get(), 12)
                    transcript.extend(data)
                    if b"\x1b[6n" in data:
                        os.write(master, b"\x1b[1;1R")
                    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", transcript.decode(errors="replace"))
                    if not sent and "gpt-5.6-sol default" in plain:
                        os.write(master, b"Run the isolated session continuation test.")
                        # Separate typing from Enter so TUI paste-burst detection
                        # cannot absorb the submission while MCP is starting.
                        await asyncio.sleep(.3)
                        os.write(master, b"\r")
                        sent = True
                    if (not approved and 'Allow the longrun MCP server to run tool "start_job"?' in plain
                            and str(root) in plain and "1. Allow" in plain):
                        os.write(master, b"\r")  # Approve this exact deterministic test operation once.
                        approved = True
                    if "SESSION_WAKE_OK" in plain:
                        break
                assert len(requests) == 3, len(requests)
                assert "[Longrun session handoff]" in json.dumps(requests[1]["input"])
                metadata = list((root / "jobs-state/jobs").glob("*.json"))
                assert len(metadata) == 1 and json.loads(metadata[0].read_text())["state"] == "succeeded"
                print(json.dumps({"result": "PASS", "real_tui": True, "launcher": args.launcher,
                                  "installed": args.installed, "artifacts": str(root)}))
            finally:
                (root / "terminal.raw").write_bytes(transcript)
                loop.remove_reader(master)
                os.close(master)
            return
        ws = await unix_connect(str(tui_socket), uri="ws://localhost/rpc", max_size=16 << 20)
        pending: dict[int, asyncio.Future] = {}
        queue = asyncio.Queue()
        sequence = 0
        async def read() -> None:
            async for raw in ws:
                message = json.loads(raw)
                if "id" in message and "method" not in message:
                    future = pending.pop(message["id"], None)
                    if future and not future.done():
                        future.set_result(message)
                elif "id" in message:
                    params = message.get("params", {})
                    allowed = (message.get("method") == "mcpServer/elicitation/request"
                               and params.get("serverName") == "longrun"
                               and params.get("_meta", {}).get("tool_params") == job_args)
                    answer = {"id": message["id"], "result": {"action": "accept", "content": {}}} if allowed else {
                        "id": message["id"], "error": {"code": -32601, "message": "unapproved test operation"}}
                    await ws.send(json.dumps(answer))
                else:
                    events.append(message)
                    await queue.put(message)
        reader_task = asyncio.create_task(read())
        async def call(method: str, params: dict) -> dict:
            nonlocal sequence
            sequence += 1
            future = asyncio.get_running_loop().create_future()
            pending[sequence] = future
            await ws.send(json.dumps({"id": sequence, "method": method, "params": params}))
            reply = await asyncio.wait_for(future, 10)
            if "error" in reply:
                raise RuntimeError(reply)
            return reply["result"]
        await call("initialize", {"clientInfo": {"name": "session_runtime_probe", "version": "1"},
                                  "capabilities": {"experimentalApi": True}})
        await ws.send('{"method":"initialized","params":{}}')
        thread_id = (await call("thread/start", {"cwd": str(root), "modelProvider": "probe", "model": "gpt-5.6-sol"}))["thread"]["id"]
        if args.resume:
            seeded = (await call("turn/start", {"threadId": thread_id, "input": [
                {"type": "text", "text": "Initialize this isolated resume fixture."}]}))["turn"]["id"]
            while True:
                message = await asyncio.wait_for(queue.get(), 10)
                if message.get("method") == "turn/completed" and message["params"]["turn"]["id"] == seeded:
                    assert message["params"]["turn"]["status"] == "completed"
                    break
            seed_mode = False
            requests.clear()
            await call("thread/unsubscribe", {"threadId": thread_id})
            await call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        assert (await call("thread/goal/get", {"threadId": thread_id}))["goal"] is None
        if goal_mode:
            test_goal = (await call("thread/goal/set", {"threadId": thread_id,
                "objective": "Complete the isolated Longrun Goal wakeup test.", "status": "active"}))["goal"]
            first_id = None
        else:
            first_id = (await call("turn/start", {"threadId": thread_id, "input": [
                {"type": "text", "text": "Run the isolated session continuation test."}]}))["turn"]["id"]
        statuses = []
        job_result = None
        while len(statuses) < (3 if args.chain else 2):
            message = await asyncio.wait_for(queue.get(), 15)
            params = message.get("params", {})
            item = params.get("item", {})
            if goal_mode and message.get("method") == "turn/started" and first_id is None:
                first_id = params["turn"]["id"]
            if goal_mode and message.get("method") == "item/completed" and item.get("tool") == "start_job":
                assert item["status"] == "completed", item.get("error")
                goal_job_id = item["result"]["structuredContent"]["job_id"]
                if args.goal_user_pause:
                    await call("thread/goal/set", {"threadId": thread_id, "status": "paused"})
            if message.get("method") == "item/completed" and item.get("tool") == "get_job":
                job_result = item["result"]["structuredContent"]
                if goal_mode:
                    await call("thread/goal/set", {"threadId": thread_id, "status": "complete"})
                    goal_completed.set()
            if message.get("method") == "turn/completed":
                statuses.append((params["turn"]["id"], params["turn"]["status"]))
                if statuses[0] != (first_id, "completed" if goal_mode else "interrupted"):
                    raise AssertionError(f"handoff did not interrupt the first turn: {statuses}; {root}")
                if goal_mode and args.goal_user_pause:
                    await asyncio.sleep(3)
                    final_goal = (await call("thread/goal/get", {"threadId": thread_id}))["goal"]
                    assert final_goal["status"] == "paused" and len(requests) == 2
                    with sqlite3.connect(root / "bridge.db") as db:
                        state, delivery = db.execute("SELECT state,delivery_state FROM wake_leases WHERE job_id=?", (goal_job_id,)).fetchone()
                    assert (state, delivery) == ("abandoned", "abandoned"), (state, delivery)
                    print(json.dumps({"result": "PASS", "policy": "goal", "manual_pause_preserved": True, "artifacts": str(root)}))
                    return
                if len(statuses) == 1 and args.user_activity:
                    await call("turn/start", {"threadId": thread_id, "input": [
                        {"type": "text", "text": "[manual takeover] Stop automatic continuation and answer this instead."}]})
        assert statuses[-1][1] == "completed" and statuses[-1][0] != first_id, statuses
        if args.chain:
            assert [s for _, s in statuses] == ["interrupted", "interrupted", "completed"], statuses
        if args.user_activity:
            await asyncio.sleep(3)
            metadata = list((root / "jobs-state/jobs").glob("*.json"))
            assert len(metadata) == 1
            job = json.loads(metadata[0].read_text())
            assert job["state"] == "succeeded" and len(requests) == 2, (job, len(requests))
            with sqlite3.connect(root / "bridge.db") as db:
                state, wake_turn = db.execute("SELECT state,wake_turn_id FROM wake_leases").fetchone()
            assert state == "abandoned" and wake_turn is None
            print(json.dumps({"result": "PASS", "user_activity": True, "job_continued": True, "artifacts": str(root)}))
            return
        expected = {"success": "succeeded", "failure": "failed", "timeout": "timed_out"}[args.outcome]
        assert job_result and job_result["state"] == expected and job_result["terminal"], job_result
        assert job_result["tail"].strip() == ("" if args.outcome == "timeout" else "SESSION_JOB_OK"), job_result
        if goal_mode:
            assert len(requests) == 4, len(requests)
            final_goal = (await call("thread/goal/get", {"threadId": thread_id}))["goal"]
            assert final_goal["status"] == "complete" and final_goal["objective"] == test_goal["objective"]
            assert final_goal["createdAt"] == test_goal["createdAt"]
            with sqlite3.connect(root / "bridge.db") as db:
                state, delivery, terminal = db.execute("SELECT state,delivery_state,terminal_state FROM wake_leases WHERE job_id=?", (goal_job_id,)).fetchone()
                trace = [(json.loads(details), stamp) for details, stamp in db.execute(
                    "SELECT details_json,created_at FROM bridge_events WHERE job_id=? ORDER BY id", (goal_job_id,))]
            assert (state, delivery, terminal) == ("delivered", "resumed", expected), (state, delivery, terminal)
            assert not any(x.get("state") == "abandoned" for x, _ in trace), trace
            activated = next(stamp for details, stamp in trace if details.get("delivery_state") == "resumed")
            finished = datetime.fromisoformat(job_result["finished_at_utc"]).timestamp()
            delay = activated - finished
            assert -.1 <= delay < 10, delay
            print(json.dumps({"result": "PASS", "policy": "goal", "normal_terminal": terminal,
                              "activation_delay_sec": round(delay, 3), "turns": statuses, "artifacts": str(root)}))
            return
        assert len(requests) == (5 if args.chain else 3), len(requests)
        before_wake = requests[-2]["input"]
        assert any(x.get("role") == "developer" and "[Longrun session handoff]" in json.dumps(x)
                   and job_result["job_id"] in json.dumps(x) for x in before_wake)
        outputs = [x for x in before_wake if x.get("call_id") == "start_call_1" and x.get("type") == "custom_tool_call_output"]
        assert outputs and "HANDOFF_MUST_NOT_RETURN" not in json.dumps(outputs), outputs
        assert (await call("thread/goal/get", {"threadId": thread_id}))["goal"] is None
        print(json.dumps({"result": "PASS", "resume": args.resume, "policy": args.policy, "outcome": args.outcome, "chain": args.chain,
                          "thread_id": thread_id, "turns": statuses, "job_id": job_result["job_id"], "artifacts": str(root)}))
    finally:
        (root / "events.json").write_text(json.dumps(events, indent=2))
        (root / "requests.json").write_text(json.dumps(requests, indent=2))
        if ws:
            await ws.close()
        if reader_task:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task
        for process in reversed(processes):
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        for log in logs:
            log.close()
        provider.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--policy", choices=("session", "auto", "goal"), default="session")
    parser.add_argument("--outcome", choices=("success", "failure", "timeout"), default="success")
    parser.add_argument("--user-activity", action="store_true")
    parser.add_argument("--chain", action="store_true")
    parser.add_argument("--tui", action="store_true")
    parser.add_argument("--launcher", action="store_true")
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--goal-user-pause", action="store_true")
    parser.add_argument("--goal-final-delay", type=float, default=0)
    options = parser.parse_args()
    if not 0 <= options.goal_final_delay <= 5:
        parser.error("--goal-final-delay must be between 0 and 5 seconds")
    if options.goal_user_pause and options.policy != "goal":
        parser.error("--goal-user-pause requires --policy goal")
    if options.policy == "goal" and (options.tui or options.launcher or options.user_activity or options.chain or options.resume):
        parser.error("the Goal probe uses its own isolated protocol lifecycle; combine only with --outcome/--installed")
    if options.launcher:
        options.tui = True
    asyncio.run(asyncio.wait_for(probe(options), 25))
