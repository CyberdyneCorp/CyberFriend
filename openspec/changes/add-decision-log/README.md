# add-decision-log

"O que decidimos sobre o deploy?" -- what the group settled on, answered from
rows extracted as conversation arrives, each citing the message that settled
it.

Decisions are read in the same model call that already reads a message for
asks, so they share its worker, watermark, backlog and cost meter. It lands in
three PRs: extraction and storage with no read path (5), the answer (6), and a
bounded backfill of history (7).

- `proposal.md` -- why, what changes, and the risk
- `design.md` -- the shared call, the row, evidence, and privacy
- `specs/decision-log/spec.md` -- the requirements
- `tasks.md` -- progress, by PR
