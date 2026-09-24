## Context

Windows are the retrieval and evidence unit: multi-author text with their
ordered message ids. `SearchQuery` carries `authors`, `since` and `until`, but
`HybridSearch.search` binds only the bounds. The ACL is in SQL: `channel_id =
ANY(:channel_ids)` from the retrieval viewer (asker intersected with the
audience). Catch-up (`app/catchup.py`) is the template: deterministic parser,
narrowed viewer, `SearchQuery` with a range, `write_answer`, dispatched in
`AskService._produce` before the general path.

## Decisions

### thread_context resolves the platform account (PR 0)

`THREAD_CONTEXT` selects the author's platform user id with the same scalar
subquery windowing uses (`MESSAGES_WITHOUT_WINDOW`): a person may carry more
than one account, and a join would repeat the message once per alias. The
integration test seeds an author whose Discord id is far from any row id.

### Time spans are calendar spans in the deployment's zone (PR 1)

`parse_span(text, now, tz) -> Span(start, end, label) | None`. Text is folded
(lowercase, accents stripped, whitespace collapsed); every rule runs, and the
longest match that names a real day wins, the earliest on a tie. So
"anteontem" is never "ontem" and "dia 21/09" is never "dia 21".

- Days, weeks and months are local calendar units, converted to UTC at local
  midnight. Weeks start on Monday; "semana passada" is the previous Monday to
  Monday. Months use the calendar month.
- "Last N days" counts today as one of them and runs up to now (1 to 366).
- "Since"/"desde"/"a partir de" right before a span runs it up to now. "Desde
  segunda" is the most recent Monday strictly before today.
- A named month or a year-less date later than today is last year's; "dia N"
  later than today is last month's. Dates are day first.
- A month needs a preposition ("em", "in", "no mês de", a since-word): bare
  "may" is a verb and "o evento de setembro" is a topic.
- Nothing that names no real day matches (31/02, "dia 0"), and a since-span
  that has not begun is None.
- A year-less dd/mm is a date only after a date cue ("dia", "em", "no",
  "on", "até", a since-word) or as the first words; "1/2 ETH", "3/4 of the
  quorum" and "score 10/10" are fractions and scores, not days.
- One span or none: a question naming two different spans ("hoje e ontem")
  or a range ("desde segunda até quarta", "entre 21/09 e 23/09", "até
  ontem") is None rather than one end of it, so it takes the ordinary path
  instead of a quietly narrower search. A span negated in passing ("não esta
  semana") is ignored.

`now` comes from `Edges.clock`; the zone from `ANSWER_TIMEZONE`, validated as
an IANA name at boot and defaulted to `America/Sao_Paulo` in the settings and
in the compose file. The existing `routing.Period` tables are unchanged: the
catch-up and obligation routes keep their rolling, UTC-cut meaning until they
are migrated deliberately.

### Author-scoped retrieval (PR 2)

With `authors` set, `search()` takes a message-level branch: `message` joined
to `person_platform_id` for the refs, left-joined to its window; filters on
the viewer's channels, `deleted_at IS NULL`, author and bounds, all in one
statement; newest first, about 200 rows. The topic is ranked inside that set
by exact cosine against the containing window's embedding (no approximate
scan, so nothing is under-returned), `ts_rank` breaking ties. Hits are grouped
by window, carrying only the author's lines and message ids, so the citation
lands on their message and the model never reads another author's words.
Messages not yet windowed are left out. One partial index,
`ix_message_author_time ON message(author_person_id, created_at) WHERE
deleted_at IS NULL`, serves this and name resolution.

### Resolving the person (PR 2, PR 3)

A mention resolves directly; "eu"/"I" resolves to the asker; a name resolves
through `people_named(viewer, name, limit)`, which only returns people with a
visible, non-deleted message in the viewer's channels. Exact full name beats a
first-name or prefix match. Zero candidates fall back to the ordinary path;
several get "Qual João? ..." with no retrieval and no model call.

### The route (PR 3)

`said_by_request(text)` is pure and returns a dataclass the planned unified
router can absorb. It defers to obligation questions and ask verbs, catch-up,
market questions, fact intents, and compound questions. The reply names who
and when, so a wrong resolution or span is visible, and an empty result is
one uniform text whatever the reason.

## Privacy

The person filter only narrows: the viewer's channels are bound into the same
statement. Candidates are computed under asker intersected with audience, so
a public "which one?" reply never names someone who only speaks in private
channels. Opted-out people have no rows and tombstones are excluded, and both
look the same as saying nothing. No withheld-evidence probe runs for the
route, as for catch-up.

## Risks

- Parser false positives ("what did the PR say") are read as names; an
  unresolved name costs nothing new, because it falls back.
- Two meanings of "last week" until the older parsers move.
- `person.display_name` holds only the latest global name.
