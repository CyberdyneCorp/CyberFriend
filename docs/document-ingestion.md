# Document ingestion — operator guide

CyberFriend indexes two kinds of document: files attached to messages in indexed
channels, and documents linked from those messages in external systems you have
explicitly configured. This page covers the parts an operator has to decide.

## Visibility comes from Discord, always

A document's visibility is that of **the channel it entered through** — where the
attachment was posted, or where the link was shared. Not Google's permissions,
not Notion's, and not the asker's own access to those systems.

A document that entered through several channels is readable by anyone permitted
to read **any one of them**, because each share was an independent decision by
the person who made it.

**The case that surprises people.** Someone can open a document directly in Drive,
ask CyberFriend about it, and be told nothing — because the document was shared
into a channel they cannot read. That is the correct direction to be surprised
in: the alternative is a bot that discloses a private channel's contents to
anyone who can open the underlying file. Expect to explain this; do not "fix" it.

The mirror case also holds: a document that is world-readable in Drive but was
shared only into `#leadership` stays restricted to `#leadership` readers here.

## The external credential is the escalation path

If the account CyberFriend uses can read documents the team cannot, then posting
a link becomes a way to make the bot read a private document aloud — and the
attacker needs no access at all, only the URL.

So:

- External fetching is **off by default** (`EXTERNAL_DOCUMENTS_ENABLED`).
- Each configured source must declare `..._CONTAINERS`: the shared drives,
  folders, or workspaces the team already has. A fetched document outside them
  is discarded as if it did not exist.
- Broad scopes (`drive`, `drive.readonly`, workspace-wide Notion access,
  domain-wide delegation) are **rejected at startup**, not at first fetch.
- A document that cannot be read is reported as unretrievable without saying
  whether it exists. "Forbidden" and "not found" are deliberately the same
  answer, and the fetch log records the coarse outcome only.

Review the credential's scope before deployment. Every other control in this
feature is irrelevant if it is wrong.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DOCUMENT_ALLOWED_FORMATS` | all | Media types to parse. Every format is attack surface; keep the list short. |
| `DOCUMENT_MAX_INPUT_BYTES` | 16 MiB | Largest file downloaded or parsed. |
| `DOCUMENT_MAX_EXTRACTED_BYTES` | 8 MiB | Largest text a file may expand into (archive bombs). |
| `DOCUMENT_MAX_PARSE_SECONDS` | 20 | Wall clock per file; the parser is **killed** at the limit. |
| `DOCUMENT_MAX_MEMORY_BYTES` | 512 MiB | Address space for the parser process. |
| `DOCUMENT_CHUNK_MAX_TOKENS` | 400 | Retrieval unit size. |
| `DOCUMENT_CHUNK_OVERLAP_TOKENS` | 60 | Overlap between neighbouring units. |
| `EXTERNAL_DOCUMENTS_ENABLED` | `false` | Global switch. Off means no outbound fetch of any kind. |
| `EXTERNAL_DOCUMENT_SOURCES` | empty | Source names, e.g. `drive notion`. |
| `EXTERNAL_<NAME>_HOSTS` | — | Hosts that belong to the source. Matched exactly or on a dot boundary. |
| `EXTERNAL_<NAME>_SCOPES` | — | The credential's granted scopes, checked against the overbroad list. |
| `EXTERNAL_<NAME>_CONTAINERS` | — | Shared drives/folders/workspaces the team already reaches. |

## Parsing untrusted files

Attachments are uploaded by anyone in the server, so parsing is hostile-input
handling:

- an **allowlist** of formats, with the format decided by **content sniffing**;
  a file whose name disagrees with its bytes is skipped, unparsed;
- limits on input size, extracted size, wall time and address space;
- external entity resolution disabled in every XML-backed format, `.docx`
  included;
- parsing in a **separate process**, killed on timeout — a parser that hangs
  cannot enforce its own limit.

A bad file is skipped and counted (`document.skipped`, `document.parse_skipped`).
It never stops ingestion for anything else.

## Document text is data, never instruction

Retrieved document text — including footnotes, the last page, and metadata
fields nobody opens — is fenced as content before it reaches a model. Text
inside a document that addresses the bot is reported as content, not obeyed.
Links inside documents are never fetched: only a link in a *message*, to a
configured source, can cause a fetch.

## Cost

One upload can produce more chunks than a month of that channel's conversation,
and every chunk is embedded. Document and conversation volumes are reported
separately (`document_cost()`); watch the document figure after a bulk upload.

## Retention, deletion and opt-out

- Deleting the message that introduced a document withdraws that share
  immediately. The document stays readable only through other live shares.
- Removing a channel from indexing scope withdraws the documents that entered
  through it.
- Retention (`purge_documents_before`) and the person-level opt-out
  (`purge_person_documents`) both cover documents, not only messages. A document
  nobody shares any more is deleted outright, chunks included.
- For external documents, reconciliation refreshes content and withdraws
  anything that can no longer be retrieved. Losing access at the source is
  treated exactly like deletion.
