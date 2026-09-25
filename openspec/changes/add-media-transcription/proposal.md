## Why

People on this server say as much in voice notes and screenshots as they type,
and none of it is remembered: capture drops attachments at the Discord
boundary, so a voice note is stored as a message with empty text and the
question "what did Joao say in the audio about the deploy?" has nothing to
find. Voice questions in a DM (add-voice-questions) are heard; what people say
to each other in channels is not.

## What Changes

- **Capture (PR 8).** `media_of` in the Discord source turns each attachment
  of an allowlisted type on the Discord CDN into a `MediaRef` on the `Message`.
  Migration 0026 adds `message_media`, one pending row per ref, keyed to its
  message with `ON DELETE CASCADE`. Nothing is recorded until an operator sets
  `MEDIA_ENABLED_AT`, and then only for messages created from that moment
  (minus `MEDIA_BACKFILL_DAYS`, default 0). No bytes are downloaded and no
  model is called.
- **Transcription (PR 9).** A `MediaWorker` in the ingest process claims
  pending audio rows, downloads through the process's HTTP transport from the
  Discord CDN only, checks the bytes, transcribes with
  `gpt-4o-mini-transcribe`, withholds contact details, redacts secrets, and
  renders the text into the message's window. Off by default behind its own
  switch, under the shared hard cap of 1,500 audio minutes a month.
- **Images (PR 10).** The same pipeline for images, returning the visible text
  and a short description. Off by default behind a separate switch, with its
  own monthly cap.
- **Per-person opt-out.** Besides the global opt-out, a person can refuse
  having their voice notes and images processed while their text stays
  indexed (PR 9).

Non-goals:

- **Storing media.** Never. Download, send, discard.
- **The document corpus.** Transcripts are message text, not documents; the
  document retrieval path is not wired in production.
- **GIF and video.**
- **Media posted before it was enabled**, unless an operator sets a backfill,
  and even then not in history already imported.

## Impact

- New table `message_media` (migration 0026); `PostgresStore.upsert_messages`
  and `tombstone_message` write it in their own transactions.
- `Message.media`, `domain/media.py`, `source.media_of`.
- Settings `MEDIA_ENABLED_AT`, `MEDIA_BACKFILL_DAYS` (ingest only); later the
  per-kind switches and caps.
- New third-party egress from PR 9 on: people's voices, and from PR 10
  screenshots, sent to the configured endpoint. Each kind is its own
  off-by-default decision.
