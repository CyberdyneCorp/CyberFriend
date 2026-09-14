## Context

Adding documents changes three things at once: what the corpus contains, what the ingestion path has to survive, and how many permission systems are in play. Each is handled separately below, because conflating them is how this kind of feature leaks.

The visibility question is the one to settle first. A Drive document has Google's permissions; a Notion page has Notion's. CyberFriend has Discord's. Trying to honour all three produces a system nobody can reason about, and where the three disagree is precisely where content escapes.

## Goals / Non-Goals

**Goals:**

- Make document contents answerable evidence, cited back to where they came from.
- Parse untrusted uploads without that becoming a way to break the service.
- Keep one permission model, not three.
- Make external fetching optional and bounded.

**Non-Goals:**

- Images, audio, video. Text-bearing formats only.
- Writing to external systems. Read-only throughout.
- Reproducing Drive's or Notion's permission models.
- Fetching arbitrary URLs.

## Decisions

### Visibility comes from Discord, always

A document inherits the visibility of the **channel it entered through** — the channel where the attachment was posted or the link was shared. Not the external system's ACL, and not the asker's own access to that system.

This is defensible because it matches what actually happened: someone chose to share that document with that channel, and that act is the disclosure decision. It is also the only rule that keeps one permission model in the system.

It has two consequences worth accepting explicitly. A document shared in `#leadership` stays restricted to `#leadership` readers even if it is world-readable in Drive — conservative, and fine. And a person who can open a document directly in Drive may not be able to retrieve it through CyberFriend — surprising, but the correct direction to be surprised in.

Where a document enters through several channels, it is readable by anyone permitted to read any one of them. That follows from each share being an independent disclosure.

### The bot's external credential must not exceed the team's access

This is the escalation path, and it is easy to miss. If the bot's Drive account can read documents the team cannot, then **posting a link becomes a way to make the bot read a private document aloud** — the attacker needs no access at all, only the URL.

So the credential is required to be scoped no more broadly than what is already shared with the team, and a document that cannot be read with it is reported as unretrievable without confirming that it exists. Per-user credentials would be stronger still, but they are a larger change; the bounded shared credential is the floor, not the ideal.

### Documents are chunked as prose, not as conversation

Conversational windowing splits on silence gaps and message counts, which is meaningless for continuous text. Documents get their own chunker: overlapping units that prefer section and heading boundaries.

Both kinds share one embedding space and one retrieval interface, so a query can match either. What must not be shared is the chunker — using window logic on a PDF produces units that break mid-sentence and retrieve badly.

### Parsing is hostile-input handling

Attachments are uploaded by anyone in the server, so the parsing path is an untrusted-input path. The known classes are archive bombs, malformed documents that hang or crash parsers, and XML external-entity resolution in office formats.

Therefore: an allowlist of formats rather than a denylist, content sniffed rather than trusted from its extension, hard limits on input size, extracted size, time and memory, external entity resolution disabled, and parser failure isolated so one bad file cannot stop ingestion. A parser that hangs must be killed, not waited on.

### Documents are a better injection vector than messages, and get no exemption

Text at the end of a 60-page PDF is invisible to everyone in the channel and fully visible to retrieval. That asymmetry makes documents the natural place to hide instructions.

The existing rule — retrieved content is data, never instruction — extends to document text with no exception, explicitly including content positioned where readers will not look, and document metadata. This is specified rather than assumed because "it's just a file we parsed" is exactly the reasoning that would create an exemption.

### External content is reconciled, not fetched once

A linked document changes after it is indexed, and an answer citing superseded content is worse than no answer. Reconciliation refreshes indexed external content and withdraws what can no longer be retrieved — whether deleted, or access revoked.

Losing access at the source is treated the same as deletion: stop returning it. Continuing to serve content from a document the bot can no longer open means serving from a cache that outlived its permission.

## Risks / Trade-offs

- **Parsing untrusted files is the largest new attack surface in the project.** Limits and isolation reduce it; they do not remove it. Keeping the format allowlist small is the highest-value control, and every format added widens exposure.
- **The external credential is the escalation path.** If it is over-scoped, every other control here is irrelevant — anyone who can post a link can read anything that account can. This deserves review before deployment, not after.
- **Cost rises sharply.** One document can produce more chunks than a month of that channel's conversation, and all of it is embedded. A few large uploads can dominate the embedding bill.
- **Visibility-by-channel will occasionally surprise people** who can open a document directly but cannot retrieve it through the bot. That is the conservative direction, and worth explaining in the operator documentation rather than fixing.
- **Governance widens meaningfully.** The archive now holds document contents, typically more sensitive than chat. Retention, disclosure and opt-out have to cover documents explicitly, and an opt-out that only covers messages is not sufficient.
- **Reconciliation lags.** Between a source change and the next reconciliation, retrieval returns stale content. Citations pointing at the live document limit the damage, since a reader can see the current version.
