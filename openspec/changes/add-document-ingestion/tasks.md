## 1. Schema and retrieval integration

- [ ] 1.1 Add `document` and `document_chunk` tables, with the entry channel denormalised so the permission predicate applies in the same query
- [ ] 1.2 Model many-to-one entry: one document may enter through several messages and channels
- [ ] 1.3 Share the embedding space and retrieval interface with conversation windows, so one query matches both
- [ ] 1.4 Add tombstones, and propagate from the introducing message's deletion
- [ ] 1.5 Test: a document readable via one channel is unreachable to a viewer who can read neither

## 2. Attachment capture

- [ ] 2.1 Retrieve attachment content at ingest time rather than storing a link that expires
- [ ] 2.2 Skip attachments in channels outside indexing scope
- [ ] 2.3 Record and bound retrieval failures; do not retry indefinitely
- [ ] 2.4 Purge a channel's documents when it leaves indexing scope

## 3. Safe parsing

- [ ] 3.1 Implement the format allowlist; sniff content rather than trusting the extension
- [ ] 3.2 Enforce input size, extracted size, time and memory limits
- [ ] 3.3 Disable external entity resolution in every XML-backed format
- [ ] 3.4 Run parsing isolated so a crash or hang cannot stop ingestion; kill on timeout rather than waiting
- [ ] 3.5 **Build a hostile-input corpus** — archive bomb, malformed PDF, external-entity document, extension/content mismatch, deeply nested structure — and assert each is skipped without stopping ingestion
- [ ] 3.6 Test that a file whose declared format does not match its content is skipped

## 4. Document chunking

- [ ] 4.1 Implement a prose chunker with overlap, distinct from conversational windowing
- [ ] 4.2 Prefer heading and section boundaries where the format exposes them
- [ ] 4.3 Record a location within the document for each chunk, for citation
- [ ] 4.4 Rebuild chunks when a document's content changes

## 5. External documents

- [ ] 5.1 Implement the source allowlist; fetch nothing outside it and nothing instructed by content
- [ ] 5.2 Implement a global switch disabling external fetching entirely
- [ ] 5.3 Implement fetchers for the configured systems, read-only
- [ ] 5.4 **Verify the configured credential is scoped no wider than the team's own access** — an over-scoped account makes posting a link a way to read private documents aloud
- [ ] 5.5 Report an unreadable document as unretrievable without confirming it exists
- [ ] 5.6 Enforce per-fetch time and size limits, and keep message ingestion unaffected when a system is unavailable
- [ ] 5.7 Record every fetch: what, from where, and which message linked it
- [ ] 5.8 Test: a link posted by someone with no access to the target does not cause the bot to disclose it

## 6. Reconciliation

- [ ] 6.1 Refresh indexed external content and re-chunk on change
- [ ] 6.2 Withdraw content that can no longer be retrieved, treating access loss the same as deletion
- [ ] 6.3 Stop returning a document when its last linking message is deleted
- [ ] 6.4 Test: superseded content is never returned after reconciliation

## 7. Injection defence

- [ ] 7.1 Fence document text as data on the same terms as message content
- [ ] 7.2 Extend the injection corpus with documents hiding instructions at the end, in footnotes, and in metadata
- [ ] 7.3 Test: none of those produce an action or alter permissions

## 8. Governance

- [ ] 8.1 Extend retention to documents
- [ ] 8.2 Extend opt-out to cover a person's uploaded documents, not only their messages
- [ ] 8.3 Document the visibility-by-channel rule and its surprising case in the operator guide
- [ ] 8.4 Report embedding cost attributable to documents separately from conversation

## 9. Verification

- [ ] 9.1 Post a document in a private channel; confirm it is unreachable to a non-member and reachable to a member, with a working citation
- [ ] 9.2 Run the hostile-input corpus against a live instance; confirm ingestion survives every case
- [ ] 9.3 Link an external document; confirm content is indexed, cited, and refreshed after a source change
- [ ] 9.4 Revoke access at the source; confirm the content stops being returned
- [ ] 9.5 Measure embedding cost for a representative document set
