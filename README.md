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
- gives Codex a tool timeout slightly longer than the server's 12-hour limit.

Start a new Codex process after configuration. Existing on-screen processes do
not hot-load new MCP servers. A saved session can be resumed in a new process:

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
without model-visible polling:

```bash
~/.local/share/codex-longrun-mcp/.venv/bin/codex-longrun \
  -C /absolute/path/to/project

~/.local/share/codex-longrun-mcp/.venv/bin/codex-longrun \
  resume -C /absolute/path/to/project SESSION_ID
```

The launcher starts the unmodified official `codex app-server`, a same-user
bridge on private Unix sockets, and the official TUI in `--remote` mode. It does
not build or replace Codex. Ordinary `codex` commands retain the manual
two-turn behavior.

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
/goal Complete [OBJECTIVE] without stopping until [VERIFIABLE END STATE]. For every reviewed, trusted, non-interactive command expected to run longer than about 30 seconds, call longrun.start_job exactly once with argv as an array, an absolute cwd, sufficient hard and no-output timeouts, and wake_policy="goal". Require automatic_wakeup=true in the returned status. After start_job returns, report the job ID and state, end the current turn, and do not call get_job, run_and_wait, a generic wait tool, write_stdin, log-tail checks, or any polling loop in that turn. The local bridge will pause this Goal while the command runs and reactivate this exact Goal only after terminal metadata is committed. In the automatically resumed turn, call longrun.get_job exactly once for the recorded job ID, analyze that terminal result, and continue the Goal. If automatic_wakeup is false or Goal wakeup setup fails, stop and report the failure instead of polling. Never pass credentials or secrets to longrun.
```

Waiting inside the bridge does not invoke the model. The initial submission and
the automatically resumed turn still use model context and tokens; this is not
a universal billing guarantee.

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

## Usage

Ask Codex to use `longrun.start_job` once for a reviewed command. Pass the
command as an argument array, not as a shell string. For example:

```text
Use longrun.start_job once for ["cargo", "test"], with cwd set to this
project. Report the job ID and end the turn without polling. Do not claim that
Codex will wake automatically.
```

The job supports:

- a hard timeout;
- an optional no-output timeout;
- expected success and failure substrings;
- a bounded result tail;
- graceful termination followed by forced process-group cleanup.

Later, request one bounded status read:

```text
Call longrun.get_job once for JOB_ID. If it is still running, report the state
and do not poll again in this turn.
```

Keep the originating interactive Codex process open while the job runs. A
one-shot `codex exec` client exits after its response and closes its MCP server;
the supervisor then terminates the command by design. This parent-death rule
prevents detached work from outliving the client that started it.

When using ordinary `codex`, pause a durable Goal manually before waiting and
resume it for the agreed result check. The MCP server alone cannot control a
Goal; only the opt-in App Server bridge can do so.

### Copy-paste Goal contract

An ordinary Codex process does not wake a paused Goal automatically. Put the
no-polling contract directly in the Goal objective, then pause and resume the
Goal from the Codex CLI as shown below.

Replace the bracketed placeholders and paste this as one command:

```text
/goal Complete [OBJECTIVE] without stopping until [VERIFIABLE END STATE]. For every reviewed, trusted, non-interactive command expected to run longer than about 30 seconds, call longrun.start_job exactly once with an absolute cwd. After start_job returns, report the job ID and state, end the turn, and do not call get_job, a generic wait tool, or any polling loop in that turn. Do not claim automatic wakeup. When I later resume this Goal and provide the job ID, call longrun.get_job exactly once. If the job is still running, report that state and end the turn without polling. Continue the Goal only after a terminal result. Never pass credentials or secrets to longrun.
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

This manual pause/resume step remains necessary until an event-driven Codex
client bridge is installed. Starting a job alone is not proof that the Goal
will wake when the process exits.

### Copy-paste uninterrupted Goal contract

Use this variant only when `longrun.run_and_wait` is explicitly exposed in the
current Codex session and that client has been verified to keep one MCP tool
call pending without re-entering the model. The Goal stays active: the command
runs inside the current turn, and Codex continues only after the same tool call
returns its terminal result.

Replace the bracketed placeholders and paste this as one command:

```text
/goal Complete [OBJECTIVE] without stopping until [VERIFIABLE END STATE]. For every reviewed, trusted, non-interactive command expected to run longer than about 30 seconds, call longrun.run_and_wait exactly once with argv as an array, an absolute cwd, and sufficient hard and no-output timeouts. Remain in that single MCP call until it returns a terminal result. Do not use longrun.start_job, longrun.get_job, a generic wait tool, write_stdin, log-tail checks, status polling, repeated tool calls, or periodic model commentary while the call is pending. Treat MCP progress notifications as UI-only progress and do not respond to them with another model turn. Continue this Goal only after the original run_and_wait call returns. Never pass credentials or secrets to longrun.
```

This pattern avoids deliberate model-visible polling and does not require
pausing the Goal. It is not a universal billing guarantee: a Codex client that
converts a pending MCP call into repeated model turns can still consume tokens.
Use the asynchronous `start_job` contract above on an unverified client.

### Progress heartbeats

Heartbeat timing is server-wide and can be changed in the
`[mcp_servers.longrun.env]` section of the Codex configuration:

| Environment variable | Default | Meaning |
| --- | ---: | --- |
| `LONGRUN_HEARTBEAT_INITIAL_SEC` | `0` | Delay before the first notification; `0` disables heartbeats |
| `LONGRUN_HEARTBEAT_INTERVAL_SEC` | `900` | Interval between later notifications |
| `LONGRUN_MAX_ACTIVE_JOBS` | `4` | Maximum live jobs across local Codex MCP server processes |

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
- private state directories and files;
- bounded logs and result tails;
- a Linux supervisor that terminates the full command process group on normal
  completion, timeout, cancellation, or abrupt MCP-parent death;
- startup recovery for incomplete metadata left by an interrupted server.

Never pass passwords, tokens, API keys, private keys, cookies, or other secrets
in arguments. Do not run commands that print secrets: command output is written
to the local job log. Redaction is best-effort metadata hygiene, not a secret
detection guarantee.

Do not use the server for interactive programs, REPLs, TUI applications,
password prompts, indefinite servers, daemons, detached jobs, untrusted
repositories, or unreviewed commands.

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
and sanitized session-polling audits.

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
