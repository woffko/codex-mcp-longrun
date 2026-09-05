# Durable Goal contract for Longrun

Include the following block when creating a new Goal for automatic Longrun
continuation. For an existing Goal, apply it as a session instruction instead
of rewriting the objective. Preserve the user's original objective,
completion criteria, token budget, and status. This is an agent instruction,
not server-side enforcement and not a guarantee of model compliance.

```text
[Longrun Goal contract v1]
For every reviewed, trusted, non-interactive command expected to exceed about 30 seconds, first verify that this Goal is active and Longrun health reports bridge_configured=true. Use longrun.start_job exactly once with wake_policy="goal" explicitly; do not use "auto", "none", run_and_wait, or a background shell as a fallback for this Goal. Require automatic_wakeup=true in the response. If readiness or registration fails, report the exact failure without claiming automatic continuation or launching a duplicate command. Do not create or reactivate a completed Goal merely to enable wakeup; follow the user's actual Goal request. After a successful submission, report only the job ID/state and end the turn immediately. Do not call get_job, wait tools, write_stdin, or log-tail tools in that submission turn. The bridge owns pause/resume. On automatic continuation, call get_job exactly once for the pending job, inspect its terminal result, and continue the original objective. A pending job is not completion or a blocker by itself. Complete the Goal only after all required jobs have terminal results and the original acceptance criteria are satisfied. An explicit user request for manual operation overrides this contract. Existing approval, secret-handling, and command-scope rules still apply.
[/Longrun Goal contract v1]
```

## Apply to an existing Goal

Read the current Goal with `thread/goal/get`. Apply the block as a user-authorized
session instruction, through normal user input or `thread/inject_items`, which
appends model-visible input without starting another turn. Verify acceptance
and verify that the Goal objective, creation time, and budget remain unchanged.
Never modify Codex's databases directly.

An empty successful injection response confirms App Server acceptance, not
that the model has already consumed the instruction or that it is rendered in
paginated UI history. Keep the policy in global agent instructions for future
processes and verify the next applicable model tool call before claiming that
live behavior has changed.

According to the [App Server Goal contract](https://learn.chatgpt.com/docs/app-server#manage-a-thread-goal),
supplying a different objective to `thread/goal/set` replaces the Goal and resets
usage accounting. Omitting status and budget does not prevent that reset. Do
not rewrite an existing objective merely to attach execution instructions.

Do not edit a Goal with a live Longrun wake lease: editing its objective changes
the identity that the bridge checks and can abandon pending delivery. Apply at
a point with no pending lease, or defer until terminal delivery has finished.
Leave completed, blocked, paused, and limited Goals unchanged unless the user
explicitly requests the corresponding change. An accepted instruction does not
prove model compliance; verify the next applicable submission and continuation.

## Future Goals

Global agent instructions should ask Codex to include this contract when the
user creates a Goal for automatic Longrun execution, and use session instructions
for an existing Goal. An ordinary request to inspect or edit files is not
permission to create a durable Goal.
If a previous Goal is complete, a later task does not implicitly reactivate it.

For a new Goal, use `/goal <original objective and acceptance criteria>` and
append the block above. Existing on-screen sessions may need their global
instructions reloaded; the contract in a newly created objective is persistent.
