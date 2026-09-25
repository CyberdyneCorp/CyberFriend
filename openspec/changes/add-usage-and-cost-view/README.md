# add-usage-and-cost-view

A Usage screen in the admin console: questions, tokens, estimated cost and
tools used, per person and per feature, read live from the Langfuse traces the
bot already exports. Admins signed in through CyberdyneAuth (never script
tokens, never operators) can also read the text of a person's questions. That
text is audited per reader, never cached by the browser, disclosed to each
person actively before it is readable, and kept for 90 days. People who opted
out, erased their data or have deletions pending are always filtered out.

- `proposal.md`: why, and what changes
- `design.md`: what traces must carry, the live read path and its filters, retention, disclosure
- `specs/usage-reporting/spec.md`: the Usage view
- `specs/tracing/spec.md`: what traces now carry, retention, and the disclosure notice
- `specs/admin-console/spec.md`: the one exception to "no corpus in the console"
- `tasks.md`: progress
