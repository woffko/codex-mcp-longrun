# Codex MCP Longrun

> [!WARNING]
> This repository is currently a design draft. It does not yet contain an
> installable MCP server. The security and process-lifecycle work described
> below must be completed before the first release.

Codex MCP Longrun is a planned local STDIO MCP server for running one bounded,
non-interactive command and waiting for it to finish without model-driven
status polling.

## Why this project exists

Long builds and test suites can outlive the initial terminal tool call. When an
agent repeatedly checks a background process with `write_stdin`, every check
can require another model turn even when nothing has changed. This adds context
traffic, consumes tokens, and may cause an otherwise healthy job to be treated
as stalled.

The proposed server moves process waiting into a deterministic local Python
process:

```text
Codex calls longrun.run_and_wait once
                  |
                  v
The local MCP server starts the command,
captures output, and waits for a terminal state
                  |
                  v
Codex receives one bounded final result
```

The local wait loop does not call a model. A command still requires an initial
tool call and a final model step after completion; the goal is to remove
intermediate polling turns, not to make command execution entirely token-free.

## Intended tools

| Tool | Purpose |
| --- | --- |
| `longrun.health` | Report server version, paths, limits, and active guardrails |
| `longrun.run_and_wait` | Run one approved non-interactive command and wait locally for completion, timeout, inactivity timeout, or cancellation |
| `longrun.read_log_tail` | Read a bounded tail for a known completed job without returning the full log to the model |

The server intentionally does not expose a `start` plus frequently polled
`status` workflow. Full command output is intended to stay in a private local
log, while the MCP result returns only structured status and a bounded tail.

## Intended use cases

- Rust, C/C++, Flutter, Android, and OpenWrt builds;
- long test suites and static-analysis runs;
- bounded packaging and artifact-generation commands;
- other trusted foreground commands that require no input after launch.

The server is not intended for:

- interactive programs, REPLs, TUI applications, or password prompts;
- `sudo` commands that may request terminal input;
- indefinite servers, daemons, or detached processes;
- untrusted repositories or unreviewed commands;
- commands that print or accept credentials.

## Security boundary

This MCP server will run commands with the permissions of the operating-system
account that runs Codex. It is not an extension of the Codex shell sandbox and
does not create a security sandbox of its own.

A configured allowed-root list can restrict the accepted working directory,
but it cannot prevent a launched command from accessing other files, networks,
or processes available to the same user. It is an accidental-misrouting guard,
not an authorization boundary.

The first release must enforce all of the following:

- `longrun.run_and_wait` always requires an explicit Codex approval prompt;
- the server starts only commands from trusted workspaces;
- child processes receive a minimal allowlisted environment rather than a copy
  of the complete Codex environment;
- secrets are rejected from tool arguments and are never intentionally written
  to metadata;
- logs and metadata use private filesystem permissions and bounded sizes;
- cancellation, timeout, MCP shutdown, and abrupt parent-process failure cannot
  leave an unmanaged process tree running;
- the initial Codex configuration keeps the server optional with
  `required = false`.

## Planned architecture

The initial target is Codex CLI on Linux and WSL:

- transport: local STDIO MCP;
- implementation: Python 3.10+ with the MCP Python SDK 2.x;
- installation: isolated virtual environment under the user's local data
  directory;
- state: private job metadata and bounded logs under the user's local state
  directory;
- registration: a standalone user-level `[mcp_servers.longrun]` entry in Codex
  `config.toml`;
- configuration: explicit tool allowlist, per-tool approvals, startup timeout,
  and a tool timeout longer than the server's maximum internal command timeout.

The server will remain separate from project-memory and language-server MCPs.
Those systems may reference a returned log path, but command execution should
remain independently auditable and independently disableable.

## Release gates

Before installation instructions are published, the implementation must pass:

1. Python compilation and dependency integrity checks.
2. In-memory MCP registration and structured-output tests.
3. Successful completion, non-zero exit, hard-timeout, and inactivity-timeout
   tests.
4. Cancellation and process-tree termination tests.
5. Abrupt MCP-parent termination and orphan-recovery tests.
6. Allowed-root and symlink-resolution tests.
7. Environment isolation and secret-sentinel tests.
8. Log-size, Unicode, truncation, and concurrent-job tests.
9. Codex configuration parsing and MCP registration checks.
10. A real acceptance run lasting longer than one minute with exactly one
    `run_and_wait` call and no model-driven status polling.

## Current status

- [x] Problem and high-level MCP workflow defined.
- [x] Codex STDIO MCP configuration and MCP Python SDK v2 feasibility reviewed.
- [ ] Hardened server implementation.
- [ ] Automated tests.
- [ ] Safe installer and rollback workflow.
- [ ] Linux/WSL acceptance testing.
- [ ] First release.

Installation is intentionally not documented yet. Do not treat the current
repository as production-ready until the release gates above are complete.

## Background

- [OpenAI Codex MCP documentation](https://developers.openai.com/codex/mcp)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Codex issue: background process polling wastes tokens](https://github.com/openai/codex/issues/13733)
- [Codex issue: event-driven wakeup for background commands](https://github.com/openai/codex/issues/32188)

