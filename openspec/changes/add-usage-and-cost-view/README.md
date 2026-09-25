# add-usage-and-cost-view

A Usage screen in the admin console: questions, tokens, estimated cost and
tools used, per person and per feature, taken from the Langfuse traces the bot
already exports. Admins (not operators) can also read the text of a person's
questions. Because that text can quote private channels and DMs, it is
admin-only, audited, never cached, disclosed to users, and kept for a stated
period.

- `proposal.md`: why, and what changes
- `design.md`: what traces must carry, the rollup, the read path, retention
- `specs/usage-reporting/spec.md`: the Usage view
- `specs/tracing/spec.md`: what traces now carry, and their retention
- `specs/admin-console/spec.md`: the one exception to "no corpus in the console"
- `tasks.md`: progress
