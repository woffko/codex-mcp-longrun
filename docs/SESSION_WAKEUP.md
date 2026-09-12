# Experimental continuation by Goal state

Requires the updated `codex-longrun` launcher. Runtime acceptance was performed
with Codex 0.153.4. Ordinary `codex` still has no wake coordinator.

| Policy | Behavior |
| --- | --- |
| `auto`, `goal`, `session` | Active Goal uses Goal continuation; paused, blocked, completed, or absent Goal uses session continuation |
| `none` | Explicit manual operation; no automatic wakeup |

With a configured bridge, registration errors fail before command startup.
Version `0.4.0a13` uses a 45-second bridge-side registration deadline and a 50-second MCP-client
deadline because one registration can require several independently bounded
Codex App Server checks. The shorter inner deadline cancels a stalled prepare
and returns a concrete setup error before the client gives up. Session mode
persists its preparing lease before the TUI checks. Aborts are also recorded
before any lease exists, so a delayed first Goal snapshot cannot create a lease
after cancellation. A disconnected preparation is cancelled and cleaned up;
handoff and terminal delivery retain their independent lifetime. Setup errors
report the job ID and the failed registration stage. A lost pause reply can
leave a Goal paused for manual recovery; the bridge never guesses that it may
override a user pause.
In this `experimental` branch trial, a paused or blocked (stalled) Goal does not
prevent an already-authorized task from using session continuation. Its status,
objective, and usage remain unchanged. Usage-limited and budget-limited Goals
still stop automatic registration. Unknown/malformed Goal states also fail.
Without a bridge, `auto` retains the manual fallback; explicit
`session` and `goal` fail before startup.

`health.state_based_routing=true` advertises this behavior; stable coordinators
retain their previous policy. `wake_policy` records the caller's hint and
`wake_mode` records the actual continuation mechanism. Selection happens at
registration; changing the Goal later still cancels the pending wake rather
than migrating an already registered job. No existing Goal is automatically
reactivated merely to enable a separate session task.

## Handoff

1. The proxy observes the originating `longrun.start_job` call. The coordinator
   correlates its thread, turn, and call ID with trusted MCP request metadata.
   Model arguments cannot select the target thread or turn.
2. The coordinator records a private SQLite lease before the command starts.
3. Longrun starts the supervised background job and keeps the MCP request
   pending. A separately owned handoff task survives cancellation of that RPC.
4. Through the owning TUI connection, the coordinator interrupts the exact
   originating turn and confirms both the completion event and its persisted
   `interrupted` state. It then injects a truthful startup receipt into the
   model-visible thread history. No extra generation is needed for the receipt.
5. Terminal job metadata triggers one completion turn through that same TUI
   connection. The model reads `get_job` once and continues the original task.

The original `functions.exec` may be displayed as interrupted. The receipt
explains that startup succeeded despite that transport interruption and names
the existing job. Never infer that the command should be launched again from
the aborted outer call alone.

Use a separate executor call containing only `start_job`. The held request
prevents sequential model/executor work after that call from proceeding before
handoff. It cannot undo unrelated work already launched in parallel within the
same executor cell. No raw-response events, arbitrary sleep-based interrupt
window, transcript rewriting, hook-trust bypass, or Codex binary patch is used.

## User control and recovery

New input, steering, interruption, thread switching, and disconnecting the owning
TUI cancel its pending session wake. They do not cancel the background command;
use `cancel_job` with the usual approval for that. `cancel_wakeup` provides an
explicit thread-scoped cancellation and reports whether a pending wake existed.

User control and internal wake requests are serialized on the owning proxy
connection. Multiple subscribed TUI clients make ownership ambiguous, so
automatic session handoff is rejected rather than moving approvals to another
client. Direct App Server clients outside the launcher are outside this control
boundary; the coordinator still rechecks the latest turn and Goal before wakeup.

The handoff has a bounded timeout. Failed or ambiguous interrupt, receipt, and
turn-start operations require manual recovery; non-idempotent starts are not
blindly retried. SQLite records `activating` before `turn/start`. If its reply is
lost after the turn actually starts, the coordinator will not launch another.
Existing leases cannot be rearmed with the same job ID.

Restarting a coordinator against the same database invalidates old session
ownership and marks pending leases `needs_manual_recovery`. The normal launcher
shuts down its supervised MCP host and removes its private runtime directory
when the coordinator exits; persistent job metadata remains available for manual
inspection. Automatic survival of a fully closed Codex process is not provided.
Goal lease recovery retains its previous behavior.

`health` reports `bridge_reachable`, `session_wakeup_supported`, and
`session_transport_ready`. `get_job` adds `wake_mode`, `handoff_state`,
`wake_delivery`, and `wake_error`, while keeping the job result available when
the coordinator is offline. Receipts contain only job identity and state, never
command output or secret stdin data.

## Agent contract

With this experimental coordinator, prefer `start_job(wake_policy="auto")`. An
active Goal follows the Goal mechanism; a paused, blocked, completed, or absent
Goal follows session continuation. Use a verified
`codex-longrun` coordinator, in its own executor call. Allow the coordinator to
end the turn. Do not poll, wait, or submit duplicate commands. On its completion
wake, call `get_job` once and continue the user's task. New user instructions take
precedence. Never create or reactivate a Goal merely to obtain wakeup. If handoff
fails after startup, use the reported job ID for recovery; do not rerun the command.

For active Goals, [the Goal contract](GOAL_LONGRUN_CONTRACT.md) still applies;
its explicit `goal` hint selects the same Goal mechanism. A paused/blocked Goal
does not require separate permission to run a task the user already requested.

## Validation

Unit tests cover early terminal events, receipt/interrupt failure, ambiguous
successful wake replies, user cancellation, ownership changes, Goal protection,
and the existing Goal bridge lifecycle. The opt-in runtime probe uses the real
installed Codex, coordinator, proxy, and Longrun with a loopback deterministic
model; it needs no API key or external model service:

```bash
.venv/bin/python tests/helpers/session_runtime_probe.py
.venv/bin/python tests/helpers/session_runtime_probe.py --resume --policy auto
.venv/bin/python tests/helpers/session_runtime_probe.py --outcome failure
.venv/bin/python tests/helpers/session_runtime_probe.py --outcome timeout
.venv/bin/python tests/helpers/session_runtime_probe.py --user-activity
.venv/bin/python tests/helpers/session_runtime_probe.py --chain
.venv/bin/python tests/helpers/session_runtime_probe.py --policy auto --existing-goal paused
.venv/bin/python tests/helpers/session_runtime_probe.py --policy goal --existing-goal blocked --chain
.venv/bin/python tests/helpers/session_runtime_probe.py --policy session --existing-goal active
.venv/bin/python tests/helpers/session_runtime_probe.py --tui
# Verify the installed package through the actual launcher and remote TUI:
~/.local/share/codex-longrun-mcp/.venv/bin/python \
  tests/helpers/session_runtime_probe.py --launcher --installed
```

The protocol probes verify model-input receipts, actual terminal job results,
native turn IDs, and the absence of extra model requests during handoff. The
TUI variants additionally drive the real terminal interface and its ordinary
single-operation approval screen in a private pseudoterminal.
