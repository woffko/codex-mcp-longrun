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
- Never pass secrets in command arguments or store secrets in integration
  documentation, Git, job logs, or Codex prompts.
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
- Do not claim that Codex will wake automatically. Use `longrun.get_job` once
  in a later turn. If it is still running, do not poll again in that turn.
- Treat `longrun.run_and_wait` as legacy compatibility mode. Current Codex
  runtimes may turn a pending blocking call into model-driven wait cycles.
- Never pass secrets. Do not use longrun for interactive commands, password
  prompts, daemons, detached processes, or unreviewed commands.
```

If the project already has `AGENTS.md`, preserve every unrelated instruction.
A same-directory `AGENTS.override.md` replaces rather than merges with
`AGENTS.md`, so do not introduce one casually.

When `/goal` is active and there is no useful work independent of the running
job, tell the user to pause the goal before waiting and resume it for the agreed
result check. The MCP server cannot wake or resume an idle goal. Do not create
busywork or short-interval status calls to keep a goal active.

## 5. Restart or resume the session

Start a new Codex process after installation or enrollment. To resume a known
session in the verified project root:

```bash
codex resume -C /absolute/path/to/project SESSION_UUID
```

The UUID selects conversation history. `-C` selects the current project root
for the new process. They solve different problems and both should be explicit
when the historical session was launched from another directory.

## 6. Verify the integration

Check the resolved Codex registration from the project:

```bash
codex -C /absolute/path/to/project mcp get longrun
```

In the new session, use `/mcp` or call `longrun.health`. Confirm that:

- the server version is the expected installed version;
- the exact project root appears in `allowed_roots`;
- shell execution remains disabled;
- heartbeat is zero unless a specific client was verified for progress;
- timeout and maximum-active-job values match the intended policy.

For an end-to-end check, submit one harmless, bounded, non-interactive command
with `start_job`. Do not use a real build merely to test registration. Let it
finish without MCP status calls, then use `get_job` exactly once and verify the
terminal result.

Run this check in an interactive Codex process that remains open. Do not use a
one-shot `codex exec` process for a long asynchronous pilot: after its final
response it closes the MCP server, and the parent-death supervisor safely
terminates the command instead of leaving detached work behind.

## 7. Hand off the result

Report:

- session UUID and executable `codex resume -C ...` command;
- enrolled exact root;
- config backup path;
- `codex mcp get longrun` and `longrun.health` results;
- any verification not performed;
- repository commit or pull request when documentation or code was published.

Multiple Codex sessions may use longrun concurrently. Each Codex process starts
its own STDIO MCP server process, while job IDs and metadata files remain
separate in the shared state directory.

## Troubleshooting

A command tool reports that `cwd` is outside `LONGRUN_ALLOWED_ROOTS`:

- resume with the intended `-C` value;
- enroll that exact project root;
- start another new Codex process.

The server is configured but missing from a resumed session:

- verify it with `codex mcp get longrun`;
- exit the old Codex process completely;
- resume from a new process instead of continuing the already running client.

The enrollment command refuses the target:

- confirm the directory exists and is absolute;
- choose the exact project, not `/` or the home directory;
- inspect an existing custom longrun configuration manually if it was not
  created by this repository's configuration script.

## References

- [OpenAI Codex MCP configuration](https://developers.openai.com/codex/mcp)
- [Codex MCP Longrun README](../README.md)
