# Event-Driven Codex Goal Wakeup

The experimental `codex-longrun` launcher provides event-driven completion
without a custom Codex build. It composes four processes:

```text
official Codex TUI --remote
            |
            v
bounded TUI compatibility proxy
            |
            v
official codex app-server on a private Unix socket
       |                         |
       | MCP tool call           | Goal JSON-RPC
       v                         v
codex-mcp-longrun <------> codex-longrun-bridge
```

The launcher exports a per-process `LONGRUN_BRIDGE_SOCKET` to App Server. The
global MCP registration names that variable in `env_vars`, so only the MCP
server spawned under this launcher receives the private bridge endpoint.
Ordinary Codex processes do not create the variable and keep manual behavior.

The TUI proxy is not part of Goal delivery. It transparently relays the
bidirectional App Server protocol, including approvals and notifications. Its
only compatibility intervention is a large Legacy
`thread/read(includeTurns=true)`: in default `auto` mode, it returns a bounded
latest-turn page or a summary with no visible turns. The Goal bridge keeps its
own direct App Server connection, and `thread/resume` remains transparent, so
the proxy changes TUI scrollback rather than the model-visible resumed context.
On the tested Codex 0.147.0 path, the TUI sends `excludeTurns=true` on resume
and follows it with the Legacy full read that the proxy intercepts.

The protection modes are:

- `auto`: protect Legacy rollouts at or above the configured local size
  threshold, request a bounded newest-turn page, and fall back to no visible
  turns;
- `omit`: return no stored visible turns for every Legacy full-history read;
- `off`: relay all reads unchanged, including potentially oversized ones.

No mode edits the stored JSONL or changes its `historyMode`.

## Protocol

1. Codex calls `start_job` with `wake_policy="goal"`.
2. Codex adds the current `threadId` to MCP request `_meta`. The server ignores
   model arguments for thread identity.
3. Before starting the command, the MCP server sends `prepare` over a private
   same-user Unix socket.
4. The bridge reads the durable Goal through `thread/goal/get`, requires status
   `active`, persists a wake lease, and changes that same Goal to `paused`.
5. Only after the pause is confirmed does the MCP server start the command and
   return `automatic_wakeup = true`.
6. The command runs under the existing process-group and parent-death
   supervision. No model call or status poll is required.
7. The MCP server atomically commits terminal job metadata, then sends one
   bounded `terminal` event containing only job ID, thread ID, and terminal
   state.
8. The bridge waits on App Server thread lifecycle notifications. It does not
   poll on a timer.
9. Once `thread/read` confirms the thread is idle, the bridge re-reads the Goal
   and verifies the original thread, creation time, objective, and paused
   status.
10. The bridge sets the Goal status to `active` exactly once. Codex Goal runtime
    starts the next turn from that transition, so the bridge never calls
    `turn/start` separately.

The resumed agent calls `get_job` once for the job ID already present in its
conversation and continues from the terminal result.

## Delivery state

Wake leases and transitions are stored in a private SQLite database for the
launcher lifetime. Each Goal can own at most one live lease. Terminal delivery
uses the following guarded states:

- `armed`: Goal is paused and the command may run;
- `waiting_for_idle`: terminal metadata exists, but the originating turn has
  not become idle yet;
- `activating`: one Goal activation request has been sent;
- `resumed`: activation was confirmed;
- `abandoned`: the user or Goal lifecycle changed the owned Goal;
- `needs_manual_recovery`: activation became ambiguous or App Server failed.

An ambiguous activation is not retried. If the first request succeeded but its
response was lost, retrying could create an extra Goal turn. Manual recovery is
safer than duplicate autonomous work.

If the MCP server disappears before reporting terminal state, the lease has a
guarded deadline derived from the command timeout and grace period. Expiry can
reactivate the Goal with a local bridge error recorded, allowing the agent to
inspect durable job state instead of remaining paused indefinitely.

## Manual overrides

The bridge abandons automatic delivery if the Goal is cleared, completed,
blocked, manually resumed, or replaced. It never recreates or rewrites the Goal
objective. `wake_policy="goal"` fails before command start when no active durable
Goal or bridge is available. `wake_policy="auto"` may fall back to ordinary
asynchronous behavior with `automatic_wakeup = false`; `wake_policy="none"`
always uses manual behavior.

## Security boundary

- App Server, TUI proxy, and bridge sockets live in a `0700` temporary directory.
- The TUI proxy and bridge sockets are `0600`; the proxy rejects a peer when
  Linux `SO_PEERCRED` identifies a different UID.
- Bridge messages are JSON lines capped at 64 KiB.
- The bridge accepts no command, argument, environment, log, or output data.
- The proxy caps normal TUI frames at 128 MiB and helper responses at 16 MiB.
- The proxy logs only thread identifiers and error metadata, never turn data.
- Job execution keeps the existing exact-root, no-shell, no-elevation, bounded
  timeout, bounded log, and process-group rules.
- Secret stdin is staged outside Codex as a private short-lived one-time file;
  only its opaque handle enters the MCP request. The server validates and
  unlinks it before passing the open fd to the child. Secret-stdin output is
  suppressed and no secret material enters bridge messages or metadata.
- The App Server transport and Goal APIs are experimental. This implementation
  is a Linux/WSL pilot, not a production daemon.

## Acceptance criteria

Audit a harmless pilot from immediately before `start_job` until the resumed
Goal turn:

- exactly one `start_job` call with `wake_policy="goal"`;
- `automatic_wakeup = true` before process start is reported;
- Goal status changes `active -> paused -> active` for the same identity;
- zero `get_job`, generic wait, `write_stdin`, or status calls before terminal
  metadata exists;
- zero `collaboration.wait_agent` calls for Longrun process waiting;
- exactly one terminal bridge event and one confirmed activation;
- no bridge `turn/start` request;
- at most one `get_job` in the resumed turn;
- correct terminal result and process-group cleanup.

Use `scripts/audit-session-polling.py` to count polling-related JSONL events
without printing prompt or response content.
