# Event-Driven Codex Wakeup

Codex MCP Longrun deliberately separates command execution from model turns.
`start_job` returns a durable job ID promptly, and the local server supervises
the process without requiring an MCP status request. This removes the forced
model-visible wait loop, but it does not wake an idle Codex thread when the job
finishes.

## Current contract

1. Codex calls `start_job` once.
2. The MCP server returns the job ID after the command is running.
3. Codex reports the job ID and ends the turn without polling.
4. A user, scheduled external action, or later resumed turn calls `get_job`
   once.

The originating interactive Codex process must remain alive under the current
supervision model. Closing that client closes the MCP server and triggers
process-group cleanup. A future persistent wakeup service would need an
explicitly managed daemon boundary rather than silently weakening this safety
property.

The server returns `automatic_wakeup = false` and a conservative recommended
check delay in job metadata. These fields describe client behavior; they are
not a timer and do not authorize repeated status calls.

## Required client-side capability

A fully event-driven integration needs an opt-in Codex client subscription that
maps one terminal local job event to one thread wakeup. The client, not the MCP
server, owns the conversation and goal lifecycle.

The minimum safe event should contain only:

- MCP server name;
- job ID;
- terminal state;
- completion timestamp.

Command output should remain in the private local job log and be fetched only
through the existing bounded result or tail tools. The wake event must not
carry command arguments, environment values, or unbounded output.

## Guardrails

- Subscription must be explicit per job; starting a job must not silently
  create an unbounded background automation.
- At most one wakeup may be delivered for a job's terminal transition.
- Reconnects must deduplicate by server name and job ID.
- Cancellation and recovered terminal states must use the same deduplication
  path.
- A paused or completed `/goal` must not be resumed without an explicit Codex
  product policy and user-visible state transition.
- Failure to deliver a wakeup must not affect the supervised command or corrupt
  terminal metadata.

## Acceptance test

Use a harmless command that runs longer than the outer executor yield interval.
Audit the resumed session JSONL from a timestamp immediately before submission.
The integration passes when:

- one `start_job` call returns promptly;
- the session contains zero model calls to a generic `wait` tool while the job
  is running;
- there are zero `get_job` calls before terminal completion;
- exactly one terminal notification wakes the intended thread;
- Codex performs at most one bounded `get_job` read after that notification;
- the final job result and process-group cleanup are correct.

The repository includes `scripts/audit-session-polling.py` to count the relevant
session events without printing prompt or response content. Until the Codex
client implements the terminal subscription, use the documented two-turn
`start_job` and later `get_job` workflow.
