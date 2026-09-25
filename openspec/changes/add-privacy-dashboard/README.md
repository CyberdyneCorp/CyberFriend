# add-privacy-dashboard

`/privacy` in Discord shows a person what CyberFriend holds about them: facts,
remembered turns, scheduled tasks, alerts, voice usage, suggestions, whether
their messages are archived and where, and how long their questions and the
bot's answers are traced. One confirm flow offers two buttons: "Delete
everything" (erase it all and keep using the bot) and "Delete everything and
stop archiving me" (erase and opt out). Both erase their Langfuse traces, media
and voice usage too, and the dashboard states honestly what is kept. Also fixes
three gaps in today's opt-out: scheduled tasks keep running, existing traces
are never withdrawn, and MCP tokens and linked-URL fetches survive.

- `proposal.md`: why, and what changes
- `design.md`: inventory, what is shown where, the erasure flow, what is kept
- `specs/privacy-dashboard/spec.md`: the dashboard and erasure
- `specs/tracing/spec.md`: withdrawing a person's traces
- `specs/scheduled-tasks/spec.md`: tasks stop on opt-out (the existing requirement, now with an explicit scenario)
- `tasks.md`: progress
