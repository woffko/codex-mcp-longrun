# Codex MCP Longrun

Codex MCP Longrun is a local STDIO MCP server that runs one bounded,
non-interactive command and waits for a terminal result without model-driven
status polling.

It is intended for builds, test suites, packaging jobs, and similar trusted
foreground commands. The local server performs the wait; Codex makes one tool
call and receives one final structured result.

```text
Codex calls longrun.run_and_wait once
                  |
                  v
The local MCP server starts the command,
captures bounded output, and waits locally
                  |
                  v
Codex receives one terminal result
```

The project is currently a Linux/WSL pilot, not a production release.

## Why use it

Repeatedly checking a long build with model-visible polling tools consumes
context and may require additional model turns even when nothing changed.
Codex MCP Longrun removes those intermediate polling turns. It does not make
the initial tool call or the final model response token-free.

## Tools

| Tool | Purpose |
| --- | --- |
| `health` | Report the version, state paths, allowed roots, and active guardrails |
| `run_and_wait` | Run one command and wait for success, failure, timeout, inactivity timeout, or cancellation |
| `read_log_tail` | Read a bounded tail for a known job ID |

There is intentionally no asynchronous `start` plus frequently polled
`status` workflow.

## Requirements

- Linux or WSL;
- [Codex CLI](https://developers.openai.com/codex/cli);
- [`uv`](https://docs.astral.sh/uv/);
- a trusted project directory for `LONGRUN_ALLOWED_ROOTS`.

The dependency lock currently installs Python MCP SDK `2.0.0` in an isolated
virtual environment.

## Install from scratch

Clone the repository and install the locked runtime:

```bash
git clone https://github.com/woffko/codex-mcp-longrun.git
cd codex-mcp-longrun
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
- allows only the three documented tools;
- configures `run_and_wait` and `read_log_tail` to require approval;
- disables shell and privilege-elevation executables;
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

## Usage

Ask Codex to use `longrun.run_and_wait` once for a reviewed command. Pass the
command as an argument array, not as a shell string. For example:

```text
Use longrun.run_and_wait once for ["cargo", "test"], with cwd set to this
project. Wait for the final result and do not poll.
```

The tool supports:

- a hard timeout;
- an optional no-output timeout;
- expected success and failure substrings;
- a bounded result tail;
- graceful termination followed by forced process-group cleanup.

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
.venv/bin/python -m unittest -v tests.test_server
```

The suite covers the STDIO handshake, environment isolation, allowed-root and
shell rejection, successful and failed commands, hard and inactivity timeouts,
log truncation, cancellation, descendant cleanup, abrupt parent death, and
metadata recovery.

## Upgrade and rollback

After pulling a reviewed update, reinstall the isolated runtime:

```bash
./scripts/install-runtime.sh
```

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
- [Codex issue: background process polling wastes tokens](https://github.com/openai/codex/issues/13733)
- [Codex issue: event-driven wakeup for background commands](https://github.com/openai/codex/issues/32188)
