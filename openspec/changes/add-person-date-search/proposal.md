## Why

"What did Bea say about the release last week?" is one of the most common
questions asked of a chat archive, and today it is answered badly. The
question goes to general retrieval, which ranks windows by topic across every
author: the answer mixes what Bea said with what others said around her, and
"last week" is either ignored or read as a rolling seven-to-fourteen days ago
cut at UTC midnight.

The pieces half exist. `SearchQuery` already has `authors`, `since` and
`until`, but the Postgres backend silently ignores `authors`. Catch-up already
shows the shape: a deterministic parser, a narrowed viewer and a cited,
grounded answer. What is missing is a time parser that speaks both languages
in the deployment's calendar, author-scoped retrieval, resolving a name to a
person the asker may know about, and the route.

Separately, `HybridSearch.thread_context` reports the internal person row id
as the author's Discord id, so the MCP `thread_context` tool names accounts
that do not exist. Author-scoped work has to start from correct person refs,
so that is fixed first.

## What Changes

- **Fix `thread_context` author ids** (PR 0): the statement resolves the
  author's platform account, as windowing already does.
- **Bilingual time spans** (PR 1): `app/timespan.py`, `parse_span(text, now,
  tz)`, pure and deterministic, PT and EN, accent-insensitive: today/hoje,
  yesterday/ontem, anteontem, this and last calendar week (Monday first), this
  and last month, the last N days, since a weekday, a named month, and dates
  (21/09, 2026-09-21, "dia 15"). Half-open UTC bounds computed in the
  deployment's zone, `ANSWER_TIMEZONE` (default `America/Sao_Paulo`).
- **Author-scoped retrieval and person resolution** (PR 2): `SearchQuery.authors`
  implemented in `HybridSearch.search` as a message-level branch with ACL,
  tombstones, author and bounds in one statement; `SearchBackend.people_named`,
  viewer-required; one partial index on `message(author_person_id,
  created_at)`.
- **The route** (PR 3): `said_by_request(text)` recognises "o que X disse
  sobre Y", "what did X say about Y" and their variants, after obligations,
  catch-up and market questions; a unique person is answered with citations to
  their own messages, several candidates get a "which one?" reply with no model
  call, and an unknown name falls back to today's path.

Non-goals:

- **Moving catch-up, obligations or wallet activity onto `timespan`.** Their
  period tables stay as they are; moving them changes what "last week" means
  for them, and is its own change.
- **Nicknames not stored as a person's display name.** Reached by @mention.
- **Per-person timezones.** One deployment zone.

## Capabilities

### New Capabilities

- `person-date-search`: time spans named in a question, what a person said in
  one, how the person is resolved, and what the asker may see.

### Modified Capabilities

- None. `thread_context`'s author id was always meant to be the platform id;
  PR 0 makes it so.

## Risk

The retrieval change touches the permission surface: the author branch binds
the viewer's channels into the same statement as its other filters, and name
resolution only offers people who have a visible message in those channels.
Until PR 2 lands, `SearchQuery.authors` stays ignored by the backend, so no
caller may rely on it. Two meanings of "last week" coexist until the older
parsers move. Adding the setting changes nothing else: nothing reads
`ANSWER_TIMEZONE` until PR 3.
