## 1. Schema

- [ ] 1.1 Add the `ask` table: source message, requester, addressee, group addressee, text, kind, status, confidence, timestamps
- [ ] 1.2 Add a correction record that overrides extracted state and survives re-extraction
- [ ] 1.3 Denormalise the source channel onto `ask` so the permission predicate applies in the same query, as for windows
- [ ] 1.4 Index by addressee and time — the shape every obligation question uses

## 2. Extraction

- [ ] 2.1 Implement the candidate filter: only windows with a plausible addressee (mention, reply, bot DM, second-person address)
- [ ] 2.2 Implement the extraction pass on the cheap model with a strict output schema
- [ ] 2.3 Extract requests, questions and commitments as distinct kinds
- [ ] 2.4 Record confidence per extraction and keep sub-threshold asks out of direct answers
- [ ] 2.5 Make extraction idempotent per source message so reprocessing does not duplicate
- [ ] 2.6 Skip bot messages, joins, and system messages

## 3. Addressee resolution

- [ ] 3.1 Resolve from explicit mention
- [ ] 3.2 Resolve from reply parent when the ask names no one else
- [ ] 3.3 Resolve from a name referenced in text where it maps to a known person
- [ ] 3.4 Record group-directed asks as such rather than picking a member
- [ ] 3.5 Record unattributed when undeterminable — **use this rather than guessing**
- [ ] 3.6 Test each resolution path independently, including the unattributed case

## 4. State

- [ ] 4.1 Close an ask when the addressee replies in-thread after it
- [ ] 4.2 Close an ask on an acknowledging reaction from the addressee
- [ ] 4.3 Mark stale after a configured period rather than closing — silently closing hides what the user wanted
- [ ] 4.4 Stop reporting an ask whose source message was deleted
- [ ] 4.5 Test: state transitions use no model call

## 5. Corrections

- [ ] 5.1 Let the addressee mark an ask done or not applicable
- [ ] 5.2 Persist corrections across re-extraction
- [ ] 5.3 Reject corrections from anyone other than the addressee
- [ ] 5.4 Test: a dismissed ask does not reappear when its window is reprocessed

## 6. Answering

- [ ] 6.1 Answer "what was asked of me" from `ask` filtered by addressee and time, with no similarity search
- [ ] 6.2 Answer "what do I need to do" from open asks plus the person's own outstanding commitments
- [ ] 6.3 Attach a citation to every reported ask
- [ ] 6.4 Report "nothing outstanding" as a successful answer
- [ ] 6.5 Apply the viewer filter so asks in unreadable channels are not returned, reported, or counted
- [ ] 6.6 Test: a restricted viewer sees no trace of asks from channels they cannot read

## 7. Evaluation

- [ ] 7.1 Hand-label a set of real messages for ask presence, kind, and addressee
- [ ] 7.2 Report precision and recall; **gate on precision** — a false obligation costs more than a missed one
- [ ] 7.3 Measure addressee-resolution accuracy separately; it is the likeliest source of wrong entries
- [ ] 7.4 Re-run against the configured endpoint before release, since structured-output reliability is stack-dependent
- [ ] 7.5 Record standing extraction cost per 10k messages
