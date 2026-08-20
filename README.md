# Codex MCP Longrun

Codex MCP Longrun is a local STDIO MCP server for bounded, non-interactive
command jobs. Its asynchronous workflow returns a job ID promptly so Codex
does not need to keep a blocking tool call alive through model-visible wait
loops. The experimental `codex-longrun` launcher can pause a durable Goal while
the command runs and reactivate that exact Goal once terminal metadata exists.

It is intended for builds, test suites, packaging jobs, and similar trusted
foreground commands. The local server validates the request, supervises the
process group, captures bounded output, and persists terminal metadata.

```text
Codex calls longrun.start_job once through codex-longrun
                  |
                  v
The local MCP server starts the command,
returns a job ID, and the bridge pauses the Goal
                  |
                  v
Codex ends the turn; the command runs without model polling
                  |
                  v
Terminal event -> idle check -> Goal reactivated once
```

The project is currently a Linux/WSL pilot, not a production release.

> [!IMPORTANT]
> This README describes the `experimental` branch and package version
> `0.4.0a4`. Its recommended Goal workflow is `codex-longrun` plus
> `start_job(wake_policy="goal")`. The manual Goal and blocking workflows are
> compatibility fallbacks and must not be combined with automatic wakeup.

## Why use it

Repeatedly checking a long build with model-visible polling tools consumes
context and may require additional model turns even when nothing changed.
`start_job` avoids keeping the original tool call pending. It does not make the
initial submission or resumed result turn token-free. In an ordinary `codex`
process it cannot wake an idle thread; the opt-in launcher adds that client-side
capability through the official experimental App Server protocol.

Current Codex clients may turn a blocking MCP call into an outer executor cell
and ask the model to wait for that cell repeatedly. `run_and_wait` remains a
compatibility tool, but the installer exposes it so a Goal can require the
verified one-call workflow explicitly. Prefer `start_job` unless the current
client has been verified to keep the original MCP call pending.

While a legacy blocking call remains pending, the server can emit a short MCP
progress heartbeat. Heartbeats are disabled by default because they do not
prevent an outer Codex executor from yielding. When explicitly enabled, a
heartbeat contains only elapsed time, time since the last output, and the
number of captured bytes; it never contains command output.

The MCP and Codex documentation does not define a billing or model-token
guarantee for progress notifications. Keep them disabled unless a specific
client has been verified to render progress without re-entering the model.

## Tools

| Tool | Purpose |
| --- | --- |
| `health` | Report the version, state paths, allowed roots, and active guardrails |
| `start_job` | Start one command and optionally arm a durable Goal wake lease |
| `get_job` | Read one bounded status or terminal result for a known job ID |
| `cancel_job` | Request process-group cancellation for one exact job ID |
| `run_and_wait` | Legacy blocking compatibility mode; exposed for explicit one-call workflows |
| `read_log_tail` | Read a bounded tail for a known job ID |

Do not repeatedly call `get_job` in the same turn. Automatic Goal wakeup is
available only when Codex was started through `codex-longrun` and the returned
status says `automatic_wakeup = true`.

## One-time secret stdin

Never put a password in `argv`, an MCP argument, an environment variable, a
prompt, or a shell command line. When a reviewed non-interactive command reads
one password or token from standard input, stage the value outside Codex in a
second terminal:

```bash
~/.local/share/codex-longrun-mcp/.venv/bin/codex-longrun-secret --confirm
```

The prompt uses `getpass`; typed characters are not echoed. Stdout contains
only a random 32-character one-time handle. Copy that handle—not the
password—into the Codex request:

```text
Run the reviewed command with longrun.start_job, the normal argv and cwd,
stdin_secret_id="ONE_TIME_HANDLE", and wake_policy="goal". Never place the
secret value itself in any tool argument.
```

The handle expires after five minutes by default and is consumed once. The
server validates the backing file's owner, `0600` mode, link count, type, age,
and size with `O_NOFOLLOW`; it opens and immediately unlinks the file, then
passes only the open descriptor to the supervised command's stdin. The handle
and secret content are not stored in job metadata.

Secret-stdin jobs always suppress stdout and stderr capture. Their log and
tail remain empty even if the command accidentally echoes the password.
Consequently, `success_contains` and `failure_contains` are rejected for these
jobs; use the exit code and non-secret result artifacts instead. A command can
still write the password into its own files or send it over the network, so
review the command itself and its artifact paths before using this feature.

This is a single finite stdin payload for a non-interactive program. Commands
that require a TTY, repeated prompts, conversational input, or privilege
elevation remain unsupported.

This mechanism prevents the value from entering Codex transcripts, MCP
arguments, process argv/environment, and Longrun output storage. Before it is
consumed, the value exists briefly in a local `0600` file. It does not defend
against root, another compromised process running as the same OS user, memory
inspection, or filesystem forensics. Use an external secret broker or run the
command manually when that stronger boundary is required.

## Requirements

- Linux or WSL;
- [Codex CLI](https://developers.openai.com/codex/cli) with App Server Unix
  transport and Goal APIs (tested with Codex CLI `0.147.0`);
- [`uv`](https://docs.astral.sh/uv/);
- a trusted project directory for `LONGRUN_ALLOWED_ROOTS`.

The dependency lock currently installs Python MCP SDK `2.0.0` in an isolated
virtual environment.

## Install from scratch

Clone the repository and install the locked runtime:

```bash
git clone https://github.com/woffko/codex-mcp-longrun.git
cd codex-mcp-longrun
git switch experimental
./scripts/install-runtime.sh
```

The default runtime location is:

```text
~/.local/share/codex-longrun-mcp/.venv
```

Register the server in Codex and replace `/absolute/path/to/project` with one
trusted project root:

```bash
./scripts/configure-codex.py \
  --config "$HOME/.codex/config.toml" \
  --command "$HOME/.local/share/codex-longrun-mcp/.venv/bin/codex-mcp-longrun" \
  --server-cwd "$HOME/.local/share/codex-longrun-mcp" \
  --state-dir "$HOME/.local/state/codex-longrun" \
  --allowed-root /absolute/path/to/project
```

The configuration script:

- refuses to overwrite an existing `mcp_servers.longrun` entry;
- creates a timestamped private backup under `~/.codex/backups`;
- keeps the MCP optional with `required = false`;
- allows only `health`, `start_job`, `get_job`, `cancel_job`, `run_and_wait`, and `read_log_tail`;
- forwards `LONGRUN_BRIDGE_SOCKET` only when the opt-in launcher sets it;
- configures command start, cancellation, and log reads to require approval;
- keeps bounded metadata-only `get_job` reads automatic;
- disables shell and privilege-elevation executables;
- disables progress heartbeats by default;
- limits all live jobs across local Codex sessions to four by default;
- keeps one-time stdin handles for 300 seconds and limits their payload to
  64 KiB by default;
- gives Codex a tool timeout slightly longer than the server's 12-hour limit.

Start a new Codex process after configuration. Existing on-screen processes do
not hot-load new MCP servers. Use the `codex-longrun` commands in the next
section for event-driven Goal wakeup. Ordinary Codex can still resume the same
saved session in manual fallback mode:

```bash
codex resume -C /absolute/path/to/project SESSION_ID
```

Verify registration:

```bash
codex mcp get longrun
codex mcp list
```

For an existing installation, apply the guarded config upgrade before
reinstalling. It adds the bridge socket passthrough and keeps `run_and_wait`
available without changing allowed roots or unrelated Codex settings:

```bash
./scripts/upgrade-codex.py --config "$HOME/.codex/config.toml" --dry-run
./scripts/upgrade-codex.py --config "$HOME/.codex/config.toml"
./scripts/install-runtime.sh
```

## Event-driven Goal launcher

Use `codex-longrun` instead of `codex` for sessions that should suspend a Goal
without model-visible polling. This is the recommended workflow on the
`experimental` branch:

```bash
~/.local/share/codex-longrun-mcp/.venv/bin/codex-longrun \
  -C /absolute/path/to/project

~/.local/share/codex-longrun-mcp/.venv/bin/codex-longrun \
  resume -C /absolute/path/to/project SESSION_ID
```

The launcher starts the unmodified official `codex app-server`, a same-user
Goal bridge, a bounded TUI compatibility proxy on private Unix sockets, and the
official TUI in `--remote` mode. It does not build or replace Codex. Ordinary
`codex` commands retain the manual two-turn behavior.

### Large Legacy session compatibility

Codex App Server defines `thread/read(includeTurns=true)` as a full-history
operation. A very large Legacy JSONL session can therefore produce a single
WebSocket response larger than the official TUI's receive limit. The launcher
now routes only TUI traffic through a local compatibility proxy:

```text
official TUI -> tui.sock proxy -> official App Server
                                      ^
                                      |
                          Goal bridge stays direct
```

The default `auto` mode forwards normal traffic unchanged. For a Legacy rollout
at least 64 MiB in size, or when its local size cannot be determined safely, it
replaces only the TUI's full `thread/read` response with the latest five turns
obtained through experimental `thread/turns/list(itemsView="full")`. If that
bounded tail is unavailable or exceeds 16 MiB, the TUI receives the thread
summary with an empty visible turn list instead of an oversized frame.

This affects visible scrollback only. `thread/resume`, live events, approvals,
server requests, and all other JSON-RPC messages remain transparent, so App
Server still loads the original session as the model-visible thread context.
The proxy never edits, migrates, truncates, or rewrites a session JSONL file.
The tested Codex 0.147.0 TUI resumes with `excludeTurns=true` and then hydrates
Legacy scrollback through `thread/read`, which is the request the proxy bounds.

Legacy pagination may still require App Server to scan the complete JSONL file
once, so the first bounded tail can take time even though its returned frame is
small. The result is a compatibility guard, not a session-file migration.

Launcher controls are consumed locally and are not passed to Codex:

```bash
# Default: protect large Legacy sessions and show a five-turn tail.
codex-longrun --longrun-legacy-history auto resume -C /project SESSION_ID

# Fastest safe fallback: show no old turns for any Legacy session.
codex-longrun --longrun-legacy-history omit resume -C /project SESSION_ID

# Disable the compatibility guard. Large sessions may fail in the TUI.
codex-longrun --longrun-legacy-history off resume -C /project SESSION_ID
```

Advanced bounds can be changed with
`--longrun-history-threshold-mib`, `--longrun-history-tail-turns`, and
`--longrun-history-timeout-sec`. Use `codex-longrun --bridge-help` for the
accepted ranges. The implementation follows the official
[Codex App Server protocol](https://developers.openai.com/codex/app-server);
both the remote TUI transport and turn pagination are experimental upstream.

Do not combine this workflow with manual `/goal pause`, manual `/goal resume`,
or `run_and_wait` for the same job. The bridge owns the Goal status transition,
and an unexpected manual transition deliberately abandons its wake lease.

For an active durable Goal, call `start_job` with `wake_policy="goal"`. The MCP
server takes the thread identity from Codex request metadata, not from a
model-controlled argument. Before the command starts, the bridge verifies the
active Goal and changes it to `paused`. After terminal metadata is committed,
the bridge waits for the current turn to become idle and changes the same Goal
to `active`. That status transition starts the next Goal turn; the bridge does
not also call `turn/start`.

Only one automatic wake lease may be active for a Goal. Clearing, completing,
blocking, editing, or manually resuming that Goal abandons the lease rather
than overriding the user. An ambiguous activation failure is never retried
automatically; resume the Goal manually in that case.

### Copy-paste automatic Goal contract

Replace the placeholders and start Codex through `codex-longrun` first:

```text
/goal Complete [OBJECTIVE] without stopping until [VERIFIABLE END STATE]. For every reviewed, trusted, non-interactive command expected to run longer than about 30 seconds, call longrun.start_job exactly once with argv as an array, an absolute cwd, sufficient hard and no-output timeouts, and wake_policy="goal". Require automatic_wakeup=true in the returned status. After start_job returns, report the job ID and state, end the current turn, and do not call get_job, run_and_wait, a generic wait tool, write_stdin, log-tail checks, or any polling loop in that turn. Do not manually pause or resume this Goal; the local bridge owns those transitions, will pause this Goal while the command runs, and will reactivate this exact Goal only after terminal metadata is committed. In the automatically resumed turn, call longrun.get_job exactly once for the recorded job ID, analyze that terminal result, and continue the Goal. If automatic_wakeup is false or Goal wakeup setup fails, stop and report the failure instead of polling. Never put a secret value in argv, MCP arguments, prompts, or environment. If one reviewed command needs one finite secret stdin payload, require the user to create a one-time handle with codex-longrun-secret and pass only that handle as stdin_secret_id; secret-stdin output is intentionally suppressed.
```

Waiting inside the bridge does not invoke the model. The initial submission and
the automatically resumed turn still use model context and tokens; this is not
a universal billing guarantee.

`collaboration.wait_agent` waits for delegated model agents; it has no
relationship to a Longrun process and must never be used to wait for a Longrun
job. The same prohibition applies to generic wait tools, `write_stdin`, log
tails, and status loops in the submission turn.

### If the Goal never pauses

Inspect the `start_job` result. A bridge-enabled submission must have all of:

```text
wake_policy = "goal"
automatic_wakeup = true
wake_delivery = "armed"
```

`wake_policy="none"` deliberately bypasses the bridge, so the Goal remains
active and native Goal continuation may immediately start another model turn.
Do not wait in that turn. If a `codex-longrun` session returns
`automatic_wakeup=false`, stop and report the setup failure instead of falling
back to `collaboration.wait_agent` or polling. Use `none` only for ordinary
Codex or an explicitly requested manual fallback.

## Enroll additional projects

The global server is visible to new Codex processes, but command tools accept
working directories only under exact trusted roots. Add another project with
the idempotent enrollment command:

```bash
./scripts/enroll-project.py \
  --config "$HOME/.codex/config.toml" \
  --allowed-root /absolute/path/to/another-project \
  --dry-run

./scripts/enroll-project.py \
  --config "$HOME/.codex/config.toml" \
  --allowed-root /absolute/path/to/another-project
```

The script creates a private backup, preserves unrelated TOML data, validates
the complete update, and refuses `/` or the current user's home directory.
Start a new Codex process after enrollment.

Codex agents performing a session or project integration should follow the
[Codex Agent Integration Runbook](docs/CODEX_AGENT_INTEGRATION.md).

## Fallback and compatibility workflows

The automatic Goal contract above is the primary `experimental` workflow. Use
the alternatives in this section only when the session was started with
ordinary `codex`, automatic wakeup setup failed before command start, or a
specific client has already been verified for one blocking MCP call. Do not
mix contracts for the same job.

The job supports:

- a hard timeout;
- an optional no-output timeout;
- expected success and failure substrings;
- a bounded result tail;
- graceful termination followed by forced process-group cleanup.

Keep the originating interactive Codex process open while the job runs. A
one-shot `codex exec` client exits after its response and closes its MCP server;
the supervisor then terminates the command by design. This parent-death rule
prevents detached work from outliving the client that started it.

### Manual fallback with ordinary Codex

An ordinary `codex` process has no bridge socket and cannot wake an idle Goal.
Use `wake_policy="none"` to make that manual behavior explicit:

```text
Use longrun.start_job exactly once for ["cargo", "test"], with an absolute cwd
and wake_policy="none". Report the job ID and end the turn without polling.
```

Later, request one bounded status read:

```text
Call longrun.get_job once for JOB_ID. If it is still running, report the state
and do not poll again in this turn.
```

For a durable Goal, the user must pause it before waiting and resume it for the
agreed result check. The MCP server alone cannot control a Goal; only the
opt-in App Server bridge can do so.

#### Copy-paste manual Goal contract

An ordinary Codex process does not wake a paused Goal automatically. Put the
no-polling contract directly in the Goal objective, then pause and resume the
Goal from the Codex CLI as shown below.

Replace the bracketed placeholders and paste this as one command:

```text
/goal Complete [OBJECTIVE] without stopping until [VERIFIABLE END STATE]. For every reviewed, trusted, non-interactive command expected to run longer than about 30 seconds, call longrun.start_job exactly once with argv as an array, an absolute cwd, sufficient hard and no-output timeouts, and wake_policy="none". After start_job returns, report the job ID and state, end the turn, and do not call get_job, run_and_wait, a generic wait tool, write_stdin, log-tail checks, or any polling loop in that turn. Do not claim automatic wakeup. When I later resume this Goal and provide the job ID, call longrun.get_job exactly once. If the job is still running, report that state and end the turn without polling. Continue the Goal only after a terminal result. Never put a secret value in argv, MCP arguments, prompts, or environment. If one reviewed command needs one finite secret stdin payload, require the user to create a one-time handle with codex-longrun-secret and pass only that handle as stdin_secret_id; secret-stdin output is intentionally suppressed.
```

After Codex reports the job ID, pause the Goal before another autonomous turn
starts:

```text
/goal pause
```

When you are ready to read the result, resume the Goal and provide the exact
job ID in the next message:

```text
/goal resume
Call longrun.get_job exactly once for JOB_ID. If it is terminal, continue the Goal from that result. If it is still running, report the state and do not poll again in this turn.
```

This manual pause/resume step is only a fallback for ordinary `codex`; do not
perform it after `start_job` has returned `automatic_wakeup=true` under
`codex-longrun`. Starting a job alone is not proof that the Goal will wake when
the process exits.

### Legacy uninterrupted Goal contract

Use this variant only when `longrun.run_and_wait` is explicitly exposed in the
current Codex session and that client has been verified to keep one MCP tool
call pending without re-entering the model. The Goal stays active: the command
runs inside the current turn, and Codex continues only after the same tool call
returns its terminal result.

Replace the bracketed placeholders and paste this as one command:

```text
/goal Complete [OBJECTIVE] without stopping until [VERIFIABLE END STATE]. For every reviewed, trusted, non-interactive command expected to run longer than about 30 seconds, call longrun.run_and_wait exactly once with argv as an array, an absolute cwd, and sufficient hard and no-output timeouts. Remain in that single MCP call until it returns a terminal result. Do not use longrun.start_job, longrun.get_job, a generic wait tool, write_stdin, log-tail checks, status polling, repeated tool calls, or periodic model commentary while the call is pending. Treat MCP progress notifications as UI-only progress and do not respond to them with another model turn. Continue this Goal only after the original run_and_wait call returns. Never put a secret value in argv, MCP arguments, prompts, or environment. If one reviewed command needs one finite secret stdin payload, require the user to create a one-time handle with codex-longrun-secret and pass only that handle as stdin_secret_id; secret-stdin output is intentionally suppressed.
```

This pattern avoids deliberate model-visible polling and does not require
pausing the Goal. It is not a universal billing guarantee: a Codex client that
converts a pending MCP call into repeated model turns can still consume tokens.
Use the event-driven `codex-longrun` contract on an unverified client.

## Progress heartbeats

Heartbeat timing is server-wide and can be changed in the
`[mcp_servers.longrun.env]` section of the Codex configuration:

| Environment variable | Default | Meaning |
| --- | ---: | --- |
| `LONGRUN_HEARTBEAT_INITIAL_SEC` | `0` | Delay before the first notification; `0` disables heartbeats |
| `LONGRUN_HEARTBEAT_INTERVAL_SEC` | `900` | Interval between later notifications |
| `LONGRUN_MAX_ACTIVE_JOBS` | `4` | Maximum live jobs across local Codex MCP server processes |
| `LONGRUN_SECRET_TTL_SEC` | `300` | Maximum age of an unconsumed one-time stdin handle |
| `LONGRUN_MAX_STDIN_SECRET_BYTES` | `65536` | Maximum staged secret stdin payload |

`longrun.health` reports the effective values. The server silently disables
heartbeats for the current job if notification delivery fails; the command
continues running and still returns its terminal result. Clients that do not
request progress simply receive no heartbeat.

Job logs and metadata are private local files under:

```text
~/.local/state/codex-longrun/jobs
```

Logs are capped at 128 MiB by the default installer configuration. The result
returns only a bounded tail and the local paths.

## Security boundary

This server runs commands with the operating-system permissions of the account
running Codex. It is not a security sandbox.

`LONGRUN_ALLOWED_ROOTS` validates the resolved working directory, but a launched
program can still access any files, networks, and processes available to the
same user. Treat the allowed-root check as a routing guard, not an authorization
boundary.

Additional safeguards include:

- shell and privilege-elevation executable rejection by default;
- no tool parameter for injecting environment variables;
- a small allowlist of inherited environment names;
- no raw argument array in job metadata, only a redacted display and digest;
- private one-time stdin handles whose values never enter MCP, argv,
  environment, metadata, logs, or tails;
- mandatory output suppression whenever secret stdin is used;
- private state directories and files;
- bounded logs and result tails;
- a Linux supervisor that terminates the full command process group on normal
  completion, timeout, cancellation, or abrupt MCP-parent death;
- startup recovery for incomplete metadata left by an interrupted server.

Never pass passwords, tokens, API keys, private keys, cookies, or other secret
values in arguments, MCP fields, prompts, or environment. Use the one-time
stdin workflow only for a reviewed command that accepts one finite stdin
payload. Normal command output is written to the local job log; secret-stdin
jobs are the exception and suppress all captured output. Redaction remains
best-effort metadata hygiene, not general secret detection.

Do not use the server for interactive programs, REPLs, TUI applications,
TTY-dependent or repeated password prompts, indefinite servers, daemons,
detached jobs, untrusted repositories, or unreviewed commands.

## Test

Create the development environment and run the integration suite:

```bash
uv sync --frozen --no-dev
.venv/bin/python -m unittest discover -s tests -v
```

The suite covers the STDIO handshake, asynchronous submission and later
terminal reads, cross-session visibility and cancellation, active-job limits,
protocol-level progress delivery,
environment isolation, allowed-root and shell rejection, successful and failed
commands, hard and inactivity timeouts, log truncation, cancellation,
descendant cleanup, abrupt parent death, metadata recovery, safe config
enrollment and upgrades, backup permissions, idempotency, broad-root rejection,
sanitized session-polling audits, and one-time secret stdin with mandatory
output suppression and no metadata/log/tail disclosure.

## Upgrade and rollback

After pulling a reviewed update, preview and apply the guarded configuration
upgrade, then reinstall the isolated runtime:

```bash
./scripts/upgrade-codex.py --config "$HOME/.codex/config.toml" --dry-run
./scripts/upgrade-codex.py --config "$HOME/.codex/config.toml"
```

```bash
./scripts/install-runtime.sh
```

The upgrade creates a private timestamped backup, preserves unrelated TOML and
explicit heartbeat policy, enables the asynchronous tool allowlist, and is
idempotent. Use `--reset-heartbeat` only when an existing nonzero heartbeat
should be changed to zero.

To disable the server without deleting local state:

```bash
codex mcp remove longrun
```

Alternatively, restore the timestamped `config.toml.before-longrun-*` backup
created under `~/.codex/backups`. Start a new Codex process after changing the
configuration.

The runtime and state directories are independent of the project repository.
Removing the MCP configuration does not remove either directory automatically.

## References

- [OpenAI Codex MCP documentation](https://developers.openai.com/codex/mcp)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Event-driven wakeup design and acceptance criteria](docs/EVENT_DRIVEN_WAKEUP.md)
- [Codex issue: background process polling wastes tokens](https://github.com/openai/codex/issues/13733)
- [Codex issue: event-driven wakeup for background commands](https://github.com/openai/codex/issues/32188)
