# add-privacy-dashboard

`/privacy` in Discord shows a person what CyberFriend holds about them: facts,
remembered turns, scheduled tasks, alerts, voice usage, suggestions, whether
their messages are archived and where, and how long their questions are
traced. A single "Delete everything" button, after a typed confirmation, opts
them out and erases it all, including their Langfuse traces. Also fixes three
gaps in today's opt-out: scheduled tasks keep running, existing traces are
never withdrawn, and MCP tokens and linked-URL fetches survive.

- `proposal.md`: why, and what changes
- `design.md`: inventory, what is shown where, the erasure flow, what is kept
- `specs/privacy-dashboard/spec.md`: the dashboard and erasure
- `specs/tracing/spec.md`: withdrawing a person's traces
- `specs/scheduled-tasks/spec.md`: tasks stop on opt-out (the existing requirement, now with an explicit scenario)
- `tasks.md`: progress
