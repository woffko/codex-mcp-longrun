# Codex Agent Integration Runbook

This runbook is for a Codex agent asked to make Codex MCP Longrun available to
another local project or a saved Codex session. It covers a host where the
server is already registered globally and a clean installation from scratch.

Do not treat a session UUID as an authorization boundary. Longrun is loaded
from Codex configuration when a new Codex process starts; project access is
controlled separately by exact entries in `LONGRUN_ALLOWED_ROOTS`.

## Safety rules

- Resolve and enroll the actual trusted project directory, not the directory
  recorded by an old session when that session was started from a broader
  location.
- Never enroll `/`, a user's home directory, or a broad workspace containing
  unrelated projects.
- Never put secret values in command arguments, MCP fields, prompts,
  environment, integration documentation, Git, or job logs. For one reviewed
  finite stdin secret, the user may stage it outside Codex with
  `codex-longrun-secret`; the agent receives only the one-time
  `stdin_secret_id` handle.
- Do not create or modify `AGENTS.md` or `AGENTS.override.md` as an installation
  side effect. Add agent guidance only when the user explicitly requests it.
- Preserve unrelated user configuration and worktree changes.
- A running Codex process does not hot-load MCP configuration changes. Start a
  new process or resume the saved session from a new process.

## 1. Identify the project root

When the user gives a session UUID, locate its JSONL record under
`~/.codex/sessions` or `~/.codex/archived_sessions` and parse the first
`session_meta` object. Record the UUID, timestamp, JSONL path, and saved `cwd`.

Then establish the actual project root from the repository, maintained handoff
files, and the user's stated target. A saved `cwd` such as the user's home
directory is evidence about how Codex was launched, not permission to enroll
that whole directory.

For a Git checkout, verify the root without changing it:

```bash
git -C /absolute/path/to/project rev-parse --show-toplevel
git -C /absolute/path/to/project status --short --branch
```

For a non-Git build tree, use its exact resolved directory and verify that it
exists. Stop if the intended root remains ambiguous.

## 2. Install and register the server

Skip this section when `codex mcp get longrun` already reports an enabled
server.

```bash
git clone https://github.com/woffko/codex-mcp-longrun.git
cd codex-mcp-longrun
./scripts/install-runtime.sh
./scripts/configure-codex.py \
  --config "$HOME/.codex/config.toml" \
  --command "$HOME/.local/share/codex-longrun-mcp/.venv/bin/codex-mcp-longrun" \
  --server-cwd "$HOME/.local/share/codex-longrun-mcp" \
  --state-dir "$HOME/.local/state/codex-longrun" \
  --allowed-root /absolute/path/to/first-project
```

`configure-codex.py` refuses to overwrite an existing `mcp_servers.longrun`
entry. Use the enrollment command below for every additional project.

For an existing installation, preview and apply the guarded asynchronous-tool
upgrade before reinstalling the runtime:

```bash
./scripts/upgrade-codex.py --config "$HOME/.codex/config.toml" --dry-run
./scripts/upgrade-codex.py --config "$HOME/.codex/config.toml"
./scripts/install-runtime.sh
```

The upgrade also keeps `run_and_wait` in the global allowlist and adds
`LONGRUN_BRIDGE_SOCKET` to the MCP environment passthrough. The variable has no
value in ordinary Codex processes; the opt-in launcher creates a private value
for its own App Server only.

When an encrypted Project Memory test asset exists, Codex should call
`project_memory_stage_test_asset_for_longrun` and pass only the returned handle
as `stdin_secret_id`. The plaintext never enters the model, and the user does
not re-enter an enrolled credential.

The installed `codex-longrun-secret` helper is only a fallback when no suitable
test asset exists. It prompts with echo disabled and prints one handle:

```bash
$HOME/.local/share/codex-longrun-mcp/.venv/bin/codex-longrun-secret --confirm
```

Pass that handle as `stdin_secret_id`; never copy the secret value into the
session. The configured defaults expire unconsumed handles after 300 seconds
and cap the payload at 64 KiB. Secret-stdin jobs return no log or tail by
design, even when the child prints its stdin.

## 3. Enroll another exact project root

Preview the update first:

```bash
./scripts/enroll-project.py \
  --config "$HOME/.codex/config.toml" \
  --allowed-root /absolute/path/to/project \
  --dry-run
```

Apply it after confirming the resolved path:

```bash
./scripts/enroll-project.py \
  --config "$HOME/.codex/config.toml" \
  --allowed-root /absolute/path/to/project
```

The command is idempotent. It resolves every root, rejects `/` and the current
user's home directory, preserves unrelated TOML data, validates the complete
result, creates a private timestamped backup under `~/.codex/backups`, and
atomically replaces the config. It prints the backup path and resulting roots.

Do not replace this workflow with an unreviewed text substitution or by setting
`LONGRUN_ALLOWED_ROOTS` to a broad parent directory.

## 4. Provide agent guidance when requested

Global MCP server instructions already tell Codex how the tools behave. When a
user explicitly wants durable project guidance, add a short section to the
appropriate instruction file without replacing existing content:

```markdown
## Long-running commands with Longrun MCP

- For reviewed, trusted, non-interactive commands expected to run longer than
  about 30 seconds, use `longrun.start_job` once when it is available.
- Pass the command as an argument array and use this project's absolute root as
  `cwd`. Report the returned job ID and end the turn without polling.
- Claim automatic wakeup only when Codex was launched through `codex-longrun`,
  `wake_policy="goal"` was requested, and the result says
  `automatic_wakeup=true`. In that mode the bridge owns pause/resume; end the
  submission turn immediately and call `longrun.get_job` exactly once only in
  the automatically resumed turn. Otherwise use `longrun.get_job` once in a
  later manually resumed turn.
- Never use `collaboration.wait_agent`, a generic wait tool, `write_stdin`, log
  tails, or status loops to wait for a Longrun job. `collaboration.wait_agent`
  waits for delegated agents, not operating-system processes.
- Use `wake_policy="none"` only with ordinary Codex or an explicitly requested
  manual fallback. In a `codex-longrun` Goal session, require
  `wake_policy="goal"` and `automatic_wakeup=true`; do not silently downgrade.
- Treat `longrun.run_and_wait` as legacy compatibility mode. Current Codex
  runtimes may turn a pending blocking call into model-driven wait cycles.
- Never pass secret values through argv, MCP fields, prompts, or environment.
  For one finite secret stdin payload, prefer
  `project_memory_stage_test_asset_for_longrun` when a suitable encrypted
  test-only asset exists; do not ask the user to re-enter it. Use a
  user-created `codex-longrun-secret` handle only as a fallback. Output is
  suppressed for that job, and text-match gates are unavailable. Do not use
  longrun for interactive or TTY-dependent prompts, daemons, detached
  processes, or unreviewed commands.
```

If the project already has `AGENTS.md`, preserve every unrelated instruction.
A same-directory `AGENTS.override.md` replaces rather than merges with
`AGENTS.md`, so do not introduce one casually.

With ordinary `codex`, tell the user to pause the Goal before waiting and resume
it for the agreed result check. With `codex-longrun`, require
`wake_policy="goal"`, report the returned job ID, end the turn, and let the
bridge own the `paused -> active` transition. Do not create busywork or
short-interval status calls to keep a Goal active.

## 5. Restart or resume the session

Start a new Codex process after installation or enrollment. To resume a known
session in the verified project root with manual completion:

```bash
cd /absolute/path/to/project
codex resume SESSION_UUID
```

To opt into event-driven Goal wakeup, resume through the installed launcher:

```bash
$HOME/.local/share/codex-longrun-mcp/.venv/bin/codex-longrun \
  resume -C /absolute/path/to/project SESSION_UUID
```

The UUID selects conversation history. For ordinary Codex, changing the shell
directory first also ensures that project-scoped `.codex/config.toml` is part
of configuration discovery. Starting with `0.4.0a6`, `codex-longrun` resolves
its `-C`/`--cd` argument before it starts App Server and uses that directory as
the real process cwd, so the launcher form is safe from another directory.

## 6. Verify the integration

Check the resolved Codex registration from the project:

```bash
cd /absolute/path/to/project
codex mcp get longrun
```

In the new session, use `/mcp` or call `longrun.health`. Confirm that:

- the server version is the expected installed version;
- the exact project root appears in `allowed_roots`;
- shell execution remains disabled;
- heartbeat is zero unless a specific client was verified for progress;
- timeout and maximum-active-job values match the intended policy.
- `bridge_configured` is true only in a session launched through
  `codex-longrun`.

For a manual end-to-end check, submit one harmless, bounded, non-interactive
command with `start_job`, let it finish without MCP status calls, then use
`get_job` exactly once. For a bridge pilot, use a durable active Goal, call
`start_job` once with `wake_policy="goal"`, require `automatic_wakeup=true`, and
verify that the next Goal turn begins only after terminal metadata exists.

Run this check in an interactive Codex process that remains open. Do not use a
one-shot `codex exec` process for a long asynchronous pilot: after its final
response it closes the MCP server, and the parent-death supervisor safely
terminates the command instead of leaving detached work behind.

## 7. Hand off the result

Report:

- session UUID and executable project-root `codex resume ...` or
  `codex-longrun resume -C ...` command;
- enrolled exact root;
- config backup path;
- `codex mcp get longrun` and `longrun.health` results;
- any verification not performed;
- repository commit or pull request when documentation or code was published.

Multiple Codex sessions may use longrun concurrently. Each Codex process starts
its own STDIO MCP server process, while job IDs and metadata files remain
separate in the shared state directory.

Each `codex-longrun` process also creates an isolated App Server, TUI proxy,
and bridge socket directory. A wake lease is bound to the trusted `threadId`
attached by Codex to the MCP request, not to the current working directory or
a model argument.

## Troubleshooting

A command tool reports that `cwd` is outside `LONGRUN_ALLOWED_ROOTS`:

- resume with the intended `-C` value;
- enroll that exact project root;
- start another new Codex process.

The server is configured but missing from a resumed session:

- verify it with `codex mcp get longrun`;
- exit the old Codex process completely;
- resume from a new process instead of continuing the already running client.

Resume reports `thread-store conflict` or `already has an active writer`:

- do not delete, migrate, or fork the session merely to bypass the lock;
- first check whether the same session is still open in another TUI;
- if a terminal was lost, inspect the exact launcher's App Server and active
  Longrun jobs before terminating anything;
- upgrade to `0.4.0a7` or newer. Its isolated supervisors terminate complete
  daemon process groups when the launcher disappears, preventing a stale App
  Server writer lock after future terminal loss.

`run_and_wait` exists on the server but is missing from a session:

- confirm `codex mcp get longrun` lists it in `enabled_tools`;
- run `scripts/upgrade-codex.py` for an older registration;
- exit the old client and resume from a new process because tool catalogs are
  session-process snapshots.

`start_job` returns `automatic_wakeup=false`:

- confirm the session was launched with `codex-longrun`, not ordinary `codex`;
- confirm `longrun.health` reports `bridge_configured=true`;
- confirm the current thread has an active durable Goal;
- do not poll when `wake_policy="goal"` fails; report the setup failure.

The enrollment command refuses the target:

- confirm the directory exists and is absolute;
- choose the exact project, not `/` or the home directory;
- inspect an existing custom longrun configuration manually if it was not
  created by this repository's configuration script.

## References

- [OpenAI Codex MCP configuration](https://developers.openai.com/codex/mcp)
- [Codex MCP Longrun README](../README.md)
