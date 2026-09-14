## Why

A great deal of what a team decides lives in a document rather than a message. Someone posts a spec, a report, or a CSV and the conversation refers to it by name for the next month; someone drops a Drive link and the discussion happens around it. Today CyberFriend indexes the sentence "here's the draft" and nothing about the draft.

This change lets documents become evidence: attachments posted in indexed channels, and external documents linked from them.

It is the largest of the outstanding changes, and the one that most changes the threat model. Parsing untrusted uploads is an attack surface in its own right. A document is also a far better place to hide instructions than a chat message, because nobody scrolls to the end of a PDF. And linking to Drive and Notion means bridging two permission systems, which is exactly where disclosure bugs live.

## What Changes

- Ingest attachments posted in indexed channels, fetching content at post time rather than relying on links that expire.
- Extract text from a bounded allowlist of formats, with size, time and resource limits.
- Chunk documents as prose, separately from conversational windowing, and retrieve both through one interface.
- Follow links to external documents (Drive, Notion) and index their content.
- Give every document the visibility of the Discord channel it entered through.
- Treat document content as data, never instruction, on the same terms as message content.
- Let operators disable external fetching entirely, and restrict which external sources are followed.

Non-goals for this change:

- Images, audio and video. Text-bearing formats only.
- Writing to external document systems. Read-only.
- Reproducing an external system's own permission model. Visibility comes from Discord.
- Following arbitrary web links. Only configured document sources.

## Capabilities

### New Capabilities

- `document-ingestion`: fetching, parsing, chunking and indexing documents, with the safety limits that parsing untrusted input requires.
- `external-document-access`: following links to configured external systems, and the credential and visibility rules that keep that from bridging permissions unsafely.

### Modified Capabilities

None. `message-retrieval` returns results from an expanded corpus, but its requirements are written in terms of results and citations rather than message-only sources, so no requirement changes.

## Impact

- **Depends on** `add-discord-chat-memory` for the corpus, permissions and retrieval.
- **New attack surface: parsing untrusted files.** Archive bombs, malformed PDFs, and XML external-entity attacks in office formats are the known classes. Parsing runs under explicit resource limits with external entity resolution disabled, and a parser failure must never take down ingestion.
- **New injection surface, and a worse one than messages.** Text hidden at the end of a long document is invisible to the people in the channel but fully visible to retrieval. The existing rule that retrieved content is data extends to document content without exception.
- **New credentials** for external document systems, and a bounded scope requirement for them: an account that can read more than the team can is an escalation path, because linking a document becomes a way to have the bot read it aloud.
- **Cost rises materially.** A single document can produce more chunks than a month of conversation in the same channel, and every chunk is embedded.
- **Governance widens.** The archive now holds document content, which is typically more sensitive than chat. Retention, disclosure and opt-out must cover documents explicitly.
