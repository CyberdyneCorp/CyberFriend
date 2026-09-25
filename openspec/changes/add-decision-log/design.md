## Context

The ask pass (`app/asks/extraction.py`) runs one model call per candidate
message, showing the model six earlier messages and the reply parent. It is
driven by `ExtractionWorker` (live) and `BacklogExtractionWorker` (history),
which share the `asks_extraction_seq` watermark; an edit bumps it, so an
edited message is read again.

## Decisions

### One call, two arrays

`OUTPUT_SCHEMA` is `{asks: [...], decisions: [{summary, topic, confidence}]}`,
both required under strict mode. The parser is tolerant: a missing or
malformed array reads as empty and never costs the other one. The DECISION
section of the prompt is narrow: record a decision only when the analysed
message itself settles a course of action; never for a proposal, a question,
one person's leaning, agreement with a fact, or a status report ("fechou a
sprint"). The summary is written in the analysed message's language, never
translated. The "being wrong costs more than silence" rule covers both.

Measured against gpt-4o-mini, the first drafts of the section recorded status
reports and fact-agreement as decisions and translated Portuguese summaries
into English; a short set of in-prompt examples (none taken from the eval
set) fixed both. The ask set's precision and recall were unchanged.

### The signal

`AddresseeSignal.DECISION` is checked after mention, reply and DM and before
the English second-person fallback. It is a cost gate, generous on purpose:
"bora" is as often an invitation as a conclusion, and the model tells them
apart. Messages that already carry an ask signal cost nothing extra. Forms
that are mostly something else and add no conclusion the others miss are left
out: "fechada" (a shop, a position), "vamos de" (travel) and "going with"
(company); "bora de", "let's go with" and "we'll go with" still match.

Measured on the golden corpus (167 English messages) the candidate count is
27 on main and 27 with the markers: no English message there gains a model
call. The DECISION section and schema add about 660 prompt tokens per call
(system prompt 335 -> 921, schema 143 -> 215, o200k). There is no real
Portuguese sample to measure the PT candidate rate on yet; `UsageMeter`
reports it once deployed. Each stored decision adds one embedding call,
which, like the window embedding worker's, is not metered.

### The row

`decision_key = f"{source_message_id}:{slug(topic)}"` -- never over the
summary, which a model rephrases between runs. `source_message_id` cascades
from `message`; `channel_id` is denormalised so the read path's ACL predicate
constrains the scan. `evidence_message_ids` is the source plus every message
the model was shown with it. `decided_at` is the source's timestamp.
`embedding` is nullable; `search_tsv` is a generated `'simple'` tsvector,
since English stemming mangles Portuguese.

### Writes

`PostgresDecisionStore.record_decisions(source, decisions)` embeds first,
outside the transaction, then upserts and prunes per source message in one
transaction. It is called even when nothing was found: that call is the prune
that withdraws a decision an edit took back. A failed embedding stores NULL
(and never overwrites a stored vector); a failed write is logged and counted
and does not cost the asks from the same message, which are written in their
own transaction first.

After an edit only the edited message is pending, so the backlog pass would
read it alone -- and a conclusion that only names what was chosen in the
message before it reads as no decision, which the prune then turns into a
withdrawal. `BacklogExtractionWorker` therefore reads each chunk's
conversation back from the corpus (`Store.extraction_context`: the six live
messages before each message in its channel, and its reply parent) and a
chunk whose conversation cannot be read is left pending rather than extracted
without it.

An edit can also remove the marker altogether, so the message stops being a
candidate and is never sent to the model. `extract_window` therefore
withdraws decisions from every message of the batch that was not a
candidate, in one statement.

### Deletion, retention, opt-out

- Hard delete of the source cascades. A user's deletion is a tombstone, which
  no cascade sees and which does not bump the extraction revision, so
  `IngestService.handle_delete` withdraws every decision whose evidence holds
  the message (`evidence_message_ids @> ARRAY[id]`, GIN-indexed; the source is
  always in its own evidence). The read path (PR 6) still requires the source
  and every evidence message to be alive, for a deletion that lands between.
- Retention deletes a decision older than the cutoff, or resting on any
  evidence message older than it -- before the messages are purged, since it
  reads them.
- Opt-out deletes decisions the person stated or whose evidence they wrote,
  before their messages are purged.

## Alternatives considered

- **A second extraction pass.** Double the calls on shared candidates, a
  second watermark and backlog. Rejected; coupling is mitigated by gating on
  both evaluation sets.
- **Evidence as a join table with foreign keys.** Cascades for free, but a
  second table to keep in step on every upsert and prune; the array is checked
  in the two purges and on read.
