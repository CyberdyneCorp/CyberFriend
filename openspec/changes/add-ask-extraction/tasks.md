## 1. Schema

- [x] 1.1 Add the `ask` table: source message, requester, addressee, group addressee, text, kind, status, confidence, timestamps
- [x] 1.2 Add a correction record that overrides extracted state and survives re-extraction
- [x] 1.3 Denormalise the source channel onto `ask` so the permission predicate applies in the same query, as for windows
- [x] 1.4 Index by addressee and time — the shape every obligation question uses

## 2. Extraction

- [x] 2.1 Implement the candidate filter: only windows with a plausible addressee (mention, reply, bot DM, second-person address)
- [x] 2.2 Implement the extraction pass on the cheap model with a strict output schema
- [x] 2.3 Extract requests, questions and commitments as distinct kinds
- [x] 2.4 Record confidence per extraction and keep sub-threshold asks out of direct answers
- [x] 2.5 Make extraction idempotent per source message so reprocessing does not duplicate
- [x] 2.6 Skip bot messages, joins, and system messages

## 3. Addressee resolution

- [x] 3.1 Resolve from explicit mention
- [x] 3.2 Resolve from reply parent when the ask names no one else
- [x] 3.3 Resolve from a name referenced in text where it maps to a known person
- [x] 3.4 Record group-directed asks as such rather than picking a member
- [x] 3.5 Record unattributed when undeterminable — **use this rather than guessing**
- [x] 3.6 Test each resolution path independently, including the unattributed case

## 4. State

- [x] 4.1 Close an ask when the addressee replies in-thread after it
- [x] 4.2 Close an ask on an acknowledging reaction from the addressee
- [x] 4.3 Mark stale after a configured period rather than closing — silently closing hides what the user wanted
- [x] 4.4 Stop reporting an ask whose source message was deleted
- [x] 4.5 Test: state transitions use no model call

## 5. Corrections

- [x] 5.1 Let the addressee mark an ask done or not applicable
- [x] 5.2 Persist corrections across re-extraction
- [x] 5.3 Reject corrections from anyone other than the addressee
- [x] 5.4 Test: a dismissed ask does not reappear when its window is reprocessed

## 6. Answering

- [x] 6.1 Answer "what was asked of me" from `ask` filtered by addressee and time, with no similarity search
- [x] 6.2 Answer "what do I need to do" from open asks plus the person's own outstanding commitments
- [x] 6.3 Attach a citation to every reported ask
- [x] 6.4 Report "nothing outstanding" as a successful answer
- [x] 6.5 Apply the viewer filter so asks in unreadable channels are not returned, reported, or counted
- [x] 6.6 Test: a restricted viewer sees no trace of asks from channels they cannot read

## 7. Evaluation

- [x] 7.1 Hand-label a set of real messages for ask presence, kind, and addressee
- [x] 7.2 Report precision and recall; **gate on precision** — a false obligation costs more than a missed one
- [x] 7.3 Measure addressee-resolution accuracy separately; it is the likeliest source of wrong entries
- [x] 7.4 Re-run against the configured endpoint before release, since structured-output reliability is stack-dependent
- [x] 7.5 Record standing extraction cost per 10k messages


## 10. Wiring (added after the fact)

These were ticked while the code they describe had no caller in any running
process: `record_reaction` and `CorrectionService` were reachable only from
their own tests. A ticked box in this file was not evidence, and this section
exists so the next reader knows that.

- [x] 10.1 Bind reaction events through the gateway so an acknowledgement reaches the store
- [x] 10.2 Give the addressee a Discord command to resolve or disown an ask
- [x] 10.3 Close an ask in the same transaction that records the tick, rather than up to five minutes later
- [x] 10.4 Reopen an ask when the acknowledgement that closed it is withdrawn
- [x] 10.5 Extract from backfilled history, not only the live stream
- [ ] 10.6 Verify each of the above against the live deployment
