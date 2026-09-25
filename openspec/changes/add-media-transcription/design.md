## Context

Attachments were dropped at `source.to_message`. The conversation path --
message, window, embedding, ACL-scoped retrieval, citation to the message id --
is live end to end, and deletion, retention and opt-out already act on
messages. The document pipeline is built but not wired into production
retrieval.

## Decisions

### Derived text belongs to the message

A voice note is an utterance by a person, at a time, in a channel. Attaching
its transcript to the message puts it in the window that message is rendered
into, so it is retrieved, cited, ACL-filtered, withdrawn and filtered by person
and date exactly like typed text. The alternative, the document corpus, would
first need a retrieval path that does not exist in production, and would cite a
"document" instead of the message.

### Capture writes metadata, keyed to a stored message (PR 8)

`media_of(raw)` keeps an attachment when its declared type (parameters
stripped, lowercased) is in the allowlist and its URL is https on
`cdn.discordapp.com` or `media.discordapp.net`:

- audio: `audio/ogg`, `audio/mpeg`, `audio/mp4`, `audio/wav`, `audio/webm`
- image: `image/png`, `image/jpeg`, `image/webp`

Audio on a message flagged `IS_VOICE_MESSAGE` (1 << 13) is `voice`; other
audio is `audio`. The host check at capture means no URL outside the closed set
is ever stored for a worker to fetch later.

`PostgresStore.upsert_messages` writes the rows in the capture transaction,
after the message:

- `INSERT ... SELECT FROM message WHERE id = :id AND deleted_at IS NULL`, so a
  message refused by the 0008 opt-out trigger, or born tombstoned, gets none.
- `ON CONFLICT (message_id, attachment_id)` refreshes `source_url` only while
  the row is pending, and never touches status or attempts: a newer read
  carries a fresher CDN URL, but a re-read is not a reason to process anything
  again. This is opportunistic. An unedited message is written once: backfill
  never re-reads imported history (its cursor only moves older) and
  reconciliation writes only messages whose revision changed. So the stored
  URL, which expires in about a day, is normally stale by the time a worker
  reads it; see "A fresh URL at download".
- Every write first deletes the rows of attachments the message no longer
  carries. Correct because every writer -- live capture, edits, backfill,
  reconciliation -- writes a full re-read of the platform's message. A
  `Message` read back from the store has no media and is never written back.
- Rows are inserted only for messages created at or after `media_since`
  (`MEDIA_ENABLED_AT - MEDIA_BACKFILL_DAYS`), or never when it is unset. The
  deletion of dropped attachments runs regardless.

`build_corpus_store` builds the store for the ingest process and for the e2e
harness standing in for it, so the moment recording starts is decided once.

### An operator names the moment

`MEDIA_ENABLED_AT` is a timestamp rather than a boolean. Members posted their
earlier voice notes without expecting them to be transcribed; naming the
moment they were told keeps a later restart, or a newly indexed channel's
backfill, from sweeping those in. It is stated in configuration rather than
recorded by the process at first start, so it cannot drift if the feature is
switched off and on.

`MEDIA_BACKFILL_DAYS` moves `media_since` earlier, but only for messages
ingest writes after that: a channel's first backfill (newly indexed or
returned to scope), edits, and messages reconciliation finds were posted while
ingest was down. History already imported is never written again, so on a
deployment whose channels are already indexed it records nothing. A one-shot
re-read of `[media_since, now)` for the indexed channels would close that gap;
it is not part of this change, and the operator docs say so.

### A fresh URL at download

The signed CDN URL on a row cannot be trusted at download time: rows recorded
before the worker is switched on, or waiting behind the monthly cap, outlive
its ~24 h. From PR 9 the worker re-fetches the message by channel and message
id through the Discord client ingest already uses for backfill and
reconciliation, immediately before downloading, and takes the attachment's
URL from that read (through `media_of`, so the host check applies again),
never the stored one. The same read settles the edge cases: a message that no longer exists is
tombstoned (its rows withdrawn), and an attachment it no longer carries loses
its row, both without downloading anything.

### Deletion paths

- Tombstone: `WITHDRAW_MEDIA_FOR_MESSAGE` in `tombstone_message`'s transaction
  sets `status = 'withdrawn'`, `text = NULL`, `source_url = ''`. A re-read of
  the deleted message cannot revive it: the insert requires a live message and
  the conflict update requires `pending`.
- Retention, opt-out purge and `PURGE_CHANNEL` delete messages; the foreign
  key cascades.
- The minutes spent on a withdrawn voice note stay charged in `media_usage`,
  so deleting notes does not hand minutes back to the monthly ceiling.

### Data model

`message_media`: `message_id` (FK, cascade), `attachment_id`,
`UNIQUE(message_id, attachment_id)`, `kind` (voice | audio | image),
`declared_type`, `filename`, `byte_size`, `source_url`, `duration_secs`,
`status` (pending | done | failed | skipped | withheld | withdrawn),
`skip_reason`, `attempts`, `next_attempt_at`, `text` (only when done, by
check constraint), `language`, `model`, `redactions`, `content_sha256`,
`processed_at`, `created_at`. Partial indexes on pending rows by
`next_attempt_at` and on done rows by `processed_at` (the monthly budget). The
processing columns are created now so PRs 9 and 10 add no migration.

### Processing (PRs 9 and 10, summary)

A `MediaWorker` claims pending rows in one statement (`FOR UPDATE SKIP
LOCKED`, message live, channel in the current scope passed as an array, author
not opted out globally or from media, attempts under 3, backoff, monthly
budget), downloads through `BoundedHttpFetcher` over the process's transport,
sniffs magic bytes, transcribes or describes, then withholds on
`states_own_contact`, redacts secrets, filters known ASR hallucinations,
truncates, marks the row done and the channel dirty. `MESSAGES_FOR_REWINDOW`
aggregates done text and `_render` appends `[audio 0:42] ...` /
`[imagem: shot.png] ...`.

## Privacy

- Only indexed guild channels. DMs, private threads, out-of-scope channels,
  bot messages and opted-out authors never become a stored message, and the
  foreign key means no media row can exist without one. This is structural,
  not a check that could be forgotten.
- Nothing is recorded before `MEDIA_ENABLED_AT`; no historical media unless an
  operator sets `MEDIA_BACKFILL_DAYS`, and then only for history ingest has
  not imported yet.
- Capture never downloads or sends anything. From PR 9, the claim re-checks
  deletion, scope, the global opt-out and the per-person media opt-out
  immediately before download, and bytes are never persisted.
- Derived text is visible exactly like the message: the same channel ACL in
  SQL, the same windows and citations.
- Deleting a message withdraws its media in the same transaction; retention,
  opt-out and channel purge cascade.
- Contact details are withheld from transcripts and visible text as they are
  from typed text; secrets are redacted and only their count is kept.
- Every outbound call goes through the process's HTTP transport, to a closed
  set of hosts (the Discord CDN; the configured media endpoint), each kind
  behind its own off-by-default switch.
- Logs carry message id, kind, status and skip reason, never text.

## Risks

- The per-person media opt-out lands with the worker (PR 9): until then rows
  are recorded for people who will opt out of processing, which is harmless
  because nothing processes them.
- The CDN URL stored at capture expires in about a day and is almost never
  renewed, because an unedited message is not written again. PR 9's worker
  must re-fetch the message for a fresh URL before every download (see "A
  fresh URL at download"); relying on the stored URL would fail every row
  recorded more than a day before it is processed, including all rows
  recorded between enabling capture and switching the worker on.
- `MEDIA_BACKFILL_DAYS` does nothing for history already imported (see "An
  operator names the moment"). An operator expecting it to sweep in the last
  N days of an existing channel would get nothing; the docs state it.
- Redaction runs on the model's output, after the bytes have left.
