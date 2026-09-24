# add-person-date-search

"O que o João disse sobre o deploy semana passada?" -- what one person said,
optionally about a topic, in a span of time, answered from the messages the
asker (and the room) may read, citing that person's own messages.

Built like catch-up: the ordinary retrieval and synthesiser, with inputs
pinned by a parser that makes no model call. It lands in four PRs: a
prerequisite bug fix (0), the time-span parser (1), author-scoped retrieval
and person resolution (2), and the route itself (3).

- `proposal.md` -- why, what changes, and the risk
- `design.md` -- spans, resolution, the author branch, and privacy
- `specs/person-date-search/spec.md` -- the requirements
- `tasks.md` -- progress, by PR
