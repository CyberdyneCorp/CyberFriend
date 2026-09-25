## Where the seam goes

`ReasoningAnswerService._recorded()` is the one place every run passes through
holding both the question and the outcome, and it already calls a sink -- the
`RunRecorder`. Tracing is a second sink at the same point rather than a wider
`RunRecord`, for one reason: `RunRecord` is what gets logged, and logs go
somewhere with different retention and different readers than a trace store.
Putting message text on it would put message text in the application log, which
is a leak nobody asked for and nobody would notice.

So: `RunRecorder` keeps its shape and its logging default. A separate
`RunTracer` port takes a `RunTrace` -- question, answer, record, evidence -- and
its default implementation does nothing. Nothing about the existing contract
moves.

## Getting the evidence to the seam

`_recorded` sees a `RunOutcome`, which carries only `evidence_window_ids`. The
`Evidence` objects -- which already hold channel, text, score, url and source
system -- live in the `EvidenceLedger` inside `finish_run`, one layer down.

Two ways across: re-fetch the windows by id in the tracer, or carry them up.
Re-fetching is a second query per run against a store the tracer has no other
reason to hold, and it reads content the run may no longer be entitled to.
Carrying them up is a field. `RunOutcome` gains `evidence: tuple[Evidence, ...]`,
populated by `finish_run` from the ledger it already has. Nothing else reads it,
and it costs no query.

## Deletion

This is the part that decides whether the feature is allowed to exist. The
project guarantees that deleted content disappears everywhere, immediately.
Copying evidence text into a second store breaks that guarantee unless the
deletion follows it.

The two halves run in different processes: the bot exports traces, `ingest`
handles `on_message_delete`. They share only the database. So the bot records
which messages a trace quoted, in a table keyed by message id, and `ingest`'s
existing `handle_delete` -- already the single funnel for tombstoning, document
withdrawal and window re-forming -- looks up the trace ids for that message and
deletes them at the destination.

Deletions that fail are kept as pending work and retried, rather than logged and
lost. The corpus deletion itself never waits on the destination: the tombstone
is applied first, and the export deletion is a consequence, not a precondition.
An unreachable trace store must not be able to stop a person deleting a message.

## Why not scope traces by viewer

There is no viewer to scope to. A trace is read by an operator studying the
system, not by a member answering a question, and the set of runs worth studying
is exactly the set that spans channels. Pretending otherwise would produce a
store that is neither safe nor useful. Instead the honest statement is made
plainly, in the proposal and in the operator documentation: the trace store
holds everything the corpus holds, and is protected accordingly.

The one exclusion is a person who has opted out of indexing. Their messages are
not archived; exporting their questions to a second store would reintroduce by
the back door exactly what the opt-out removed.

## Failure posture

Tracing is observability, so it fails open: an unreachable destination produces
a log line, never an error to the requester and never a delayed reply. The
export is bounded by its own timeout and does not extend the answer path. This
is the opposite of the egress guard's posture, and deliberately so -- the guard
decides whether something may leave, while the tracer only records what already
happened.
