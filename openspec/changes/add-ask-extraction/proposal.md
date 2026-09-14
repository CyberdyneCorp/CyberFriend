## Why

*"What did people ask me today?"* and *"what do I need to do today?"* are the questions CyberFriend exists for, and retrieval alone answers them badly. Mention capture finds `@leo` but misses "can someone on the platform team look at this", "Leo said he'd handle it", and "following up on the thing from Tuesday" — no mention, still an obligation. Meanwhile similarity search is the wrong instrument entirely: these are questions about *state* — who asked, of whom, is it still open — not about topical resemblance.

Answering them by reasoning over raw retrieval is possible and unreliable. It re-derives the same conclusions on every query, costs a full run each time, and quietly misses things because the evidence was never retrieved. Extracting the obligations once, as they arrive, turns the common question into a lookup.

## What Changes

- Extract asks from conversation as it is ingested: requests, questions, and commitments, each with who asked, who it fell to, and what was asked.
- Resolve the addressee even when nobody was mentioned — from replies, thread context, and named reference.
- Track whether an ask is still open, using observable signals rather than inference.
- Record confidence, and keep low-confidence extractions out of direct answers.
- Answer obligation questions from these records rather than from retrieval.
- Let a person correct the record when the system got it wrong.

Non-goals for this change:

- Scheduled or proactive digests. Everything here still answers a question someone asked; pushing unprompted summaries is a separate decision with its own consent question.
- Task-tracker integration. Writing extracted asks into Linear or GitHub is an action on an external system and belongs to the federation change's confirmation rules.
- Inferring priority or urgency beyond what was actually said.

## Capabilities

### New Capabilities

- `ask-extraction`: identifying requests, questions and commitments in conversation, attributing them to people, tracking whether they remain open, and answering obligation questions from those records.

### Modified Capabilities

None.

## Impact

- **Depends on** `add-discord-chat-memory` for the corpus, identities, and mention records.
- **New recurring cost.** Extraction runs over new conversation continuously rather than per query. It is restricted to windows with a plausible addressee and uses the cheap model, but it is a standing spend proportional to server traffic, not to usage.
- **Precision matters more than recall.** A false "you need to do X" erodes trust faster than a missed one, because the user cannot tell which entries to doubt. Thresholds and the correction path exist for this.
- **Extraction quality is a model-dependent property**, and the configured endpoint may be a self-hosted model whose structured-output reliability differs from a hosted one. This needs its own labelled evaluation rather than assumed competence.
- **A new privacy surface.** The corpus already records what people said; this records *what people owe each other*, inferred rather than stated. It is more sensitive than the underlying messages and inherits the same channel permissions, with no exceptions.
