#!/usr/bin/env python3
"""Count model-visible polling events in one Codex session JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit a Codex session for model-visible wait polling.")
    parser.add_argument("session_log")
    parser.add_argument("--since", default="", help="Include events at or after this ISO-8601 timestamp.")
    parser.add_argument("--job-id", default="", help="Optionally count records containing one longrun job ID.")
    args = parser.parse_args()

    path = Path(args.session_log).expanduser().resolve(strict=True)
    counters = {
        "records": 0,
        "model_wait_calls": 0,
        "token_events": 0,
        "agent_messages": 0,
        "start_job_mentions": 0,
        "get_job_mentions": 0,
        "run_and_wait_mentions": 0,
        "job_id_mentions": 0,
    }
    first_timestamp: str | None = None
    last_timestamp: str | None = None

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise SystemExit(f"invalid JSON on line {line_number}: {exc}") from exc
            timestamp = record.get("timestamp")
            if not isinstance(timestamp, str) or timestamp < args.since:
                continue
            counters["records"] += 1
            first_timestamp = first_timestamp or timestamp
            last_timestamp = timestamp
            payload = record.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            if (
                record.get("type") == "response_item"
                and payload.get("type") in {"function_call", "custom_tool_call"}
                and payload.get("name") == "wait"
            ):
                counters["model_wait_calls"] += 1
            if record.get("type") == "event_msg" and payload.get("type") == "token_count":
                counters["token_events"] += 1
            if record.get("type") == "event_msg" and payload.get("type") == "agent_message":
                counters["agent_messages"] += 1

            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            counters["start_job_mentions"] += int("start_job" in serialized)
            counters["get_job_mentions"] += int("get_job" in serialized)
            counters["run_and_wait_mentions"] += int("run_and_wait" in serialized)
            if args.job_id:
                counters["job_id_mentions"] += int(args.job_id in serialized)

    print(
        json.dumps(
            {
                "session_log": str(path),
                "since": args.since or None,
                "first_timestamp": first_timestamp,
                "last_timestamp": last_timestamp,
                **counters,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
