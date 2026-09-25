# add-run-tracing

Export each run -- the question as asked, the answer as sent, the decision
trail and the retrieved evidence -- to a Langfuse deployment, so there is
something to look at when somebody says the assistant got it wrong.

Off unless an operator configures a destination. The trade it makes is stated
rather than hidden: the trace store holds private-channel content with no
viewer scoping, so it has to be protected the way the database is. What the
change does keep true across the copy is the project's two hardest guarantees
-- a deleted message deletes the traces quoting it, and a person who has opted
out is never exported.

- `proposal.md` -- why, what changes, and the risk
- `design.md` -- where the seam goes, and why deletion decides the shape
- `specs/tracing/spec.md` -- the requirements
- `tasks.md` -- progress
