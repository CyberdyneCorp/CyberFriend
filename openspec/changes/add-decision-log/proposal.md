## Why

"What did we decide about the deploy?" is a question about a conclusion, and a
conclusion is one line in a long thread. Similarity search over windows finds
the thread and leaves the reader to find the line -- and it cannot tell "que
tal deploy na sexta?" from "fechou, deploy na sexta", which use the same words
and mean opposite things.

Asks already solve the same shape of problem: extract once on ingest, store
rows that cite their source, answer by lookup. The ask pass makes one model
call per candidate message; reading that message for a decision too costs a
few prompt tokens, where a second pass would double the calls, the watermark,
the backlog and the rules.

## What Changes

- **Extraction and storage** (PR 5): the extraction schema becomes
  `{asks, decisions}`, both required, and the system prompt gains a narrow
  DECISION section. `AskExtractor.extract` returns `Extraction(asks,
  decisions)`. The candidate filter gains a bilingual `DECISION` signal
  (`DECISION_MARKERS`: fechou, fechado, bora, combinado, ficou decidido,
  vamos com, we decided, let's go with, agreed...), because the rest of the
  filter is English-only and a Portuguese conclusion addressed to nobody would
  otherwise never be read. A new `decision` table (migration 0024) holds one
  row per source message and topic, embedded at write time. Retention and
  opt-out reach it, with counts in their reports. No read path.
- **The answer** (PR 6): "o que decidimos sobre Y?" / "what did we decide
  about Y?" answered from the rows, dated and cited, with zero chat-model
  calls; below a similarity floor it falls through to retrieval.
- **Backfill** (PR 7): an operator command re-queues marker-bearing history
  since a date for the existing backlog worker.

Non-goals:

- **Supersession.** Decisions are listed dated, newest first; v1 does not
  model one replacing another.
- **Corrections.** Nobody can retract a decision except by editing or
  deleting the message it came from.
- **Participants.** The author and the cited evidence are enough to check a
  decision, and a participant list would widen what opt-out has to reach.

## Impact

- Data model: new `decision` table; `CorpusPurge` and `PersonPurge` gain a
  `decisions` count, and the admin opt-out response reports it.
- Extraction cost: about 580 more system-prompt tokens per existing ask call
  (the design estimated 150; the examples that fixed precision cost the rest),
  and new calls only for messages that carry a decision marker and no ask
  signal. One embedding per stored decision.
- Ask quality: the prompt is shared, so the ask evaluation set gates the
  change alongside a new PT/EN decision set (precision gate 0.9 each).

## Risk

- A decision agreed further back than the six context messages, or by a
  reaction, is missed. It is missed, not fabricated.
- The summary can carry the words of a context message; without the evidence
  array, deleting or opting out of the proposal would still leak it.
- Cross-language matching on the read path depends on the embedding model.
