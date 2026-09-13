## Context

The corpus can already retrieve what was said. This change records what was *asked*, which is a different kind of fact: it has parties, and it has state that changes after the message was written.

That difference is the whole rationale. "What do I need to do today" is a query over obligations filtered by person and status. Expressing it as similarity search means embedding a question whose answer shares no vocabulary with the messages that contain it, and hoping the right windows surface. Extracting once, on ingest, turns the most-asked question into a lookup.

## Goals / Non-Goals

**Goals:**

- Catch obligations that carry no mention, since those are most of them.
- Track open/closed from observable events rather than from judgement.
- Be wrong rarely, and be correctable when wrong.
- Keep the same permission guarantees as the underlying messages.

**Non-Goals:**

- Proactive digests. Pushing unprompted summaries is a consent decision, not a technical extension.
- Writing into task trackers — that is an action on an external system and belongs under the federation change's confirmation rules.
- Guessing priority or urgency beyond what was said.

## Decisions

### Extraction runs on ingest, not on query

A standing batch over new windows, on the cheap model, emitting a fixed schema. The alternative — extracting at query time — repeats identical work on every question, costs a full run each time, and can only see what retrieval happened to surface.

Cost is controlled by restricting extraction to windows with a plausible addressee: a mention, a reply, a DM to the bot, or second-person address in a channel where the candidate is active. Most channel traffic is not an obligation and should not be paid for as though it might be.

### Precision over recall, deliberately

A missed ask costs the user a thing they already knew about. A fabricated ask costs them trust in every entry, because they cannot tell which to doubt. Once a person distrusts the list they stop reading it, and the feature is dead.

So: a confidence threshold below which extractions are not presented as obligations, citations on every reported ask so the reader can check it in one click, and an unattributed state used in preference to guessing an addressee.

### State transitions come from events, not from a model

Whether an ask is still open is decided by things that either happened or did not: the addressee replied in-thread after it, or reacted with an acknowledging reaction. Asking a model "does this look resolved?" is more expensive, less consistent, and unauditable.

Ageing is handled as a third state rather than as closure. An ask nobody answered in three weeks is *stale*, not *done* — silently closing it hides exactly the thing the user wanted to know.

### Corrections outrank extraction, permanently

A person can say an ask is not theirs, or is finished, and that must survive re-extraction. Without a persisted override, an ask the user dismissed reappears the next time its window is reprocessed — which reads as the system ignoring them.

Corrections apply only to the addressee's own asks. Otherwise closing someone else's obligations becomes a way to hide them.

### Extracted asks inherit channel permissions exactly

An ask is derived from a message and carries that message's visibility, with no exceptions. This matters more than for raw messages: an ask is a *summary* of a private conversation, and summaries leak in ways quotes do not — they travel, they read as neutral fact, and they lose the context that would have signalled sensitivity. The same storage-layer filter applies, and asks in unreadable channels are not returned, reported, or counted.

### Extraction quality is measured, not assumed

The configured endpoint may be self-hosted, and structured-output reliability varies by serving stack. So this needs a labelled set and reported precision and recall, with precision the metric that gates release. Assuming competence here produces a feature that looks like it works and is wrong often enough to be worse than nothing.

## Risks / Trade-offs

- **Standing cost proportional to traffic, not usage.** A busy server pays for extraction whether or not anyone asks. The addressee-plausibility filter is the main control; if cost is still wrong, the next lever is narrowing which channels are extracted, not lowering quality.
- **Addressee resolution is the hard part** and the most likely source of wrong entries — group asks, ambiguous names, and "someone should look at this" all resolve badly. The unattributed state is the pressure valve, and it must be used rather than avoided.
- **Extraction over a private conversation produces a more portable artefact than the conversation itself.** Permission inheritance is specified tightly for this reason, but the underlying risk is that a summary is easier to mis-handle than a quote.
- **Models disagree with themselves across runs.** Re-extraction must not churn the list; corrections persist, and stable identity on the source message is what keeps an ask from being recreated as a duplicate.
- **This is inference about people, recorded.** "Leo owes Hezron an answer" is a claim the system made, not something anyone said. It should be visibly attributable to its source message, and correctable by the person it concerns — both specified — but it remains a category of data the team should know exists.
