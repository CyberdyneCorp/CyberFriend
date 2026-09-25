## ADDED Requirements

### Requirement: Channel media is recorded only once an operator enables it

The system SHALL NOT record any attachment as media unless `MEDIA_ENABLED_AT`
is set, and SHALL then record only attachments of messages created at or after
`MEDIA_ENABLED_AT` minus `MEDIA_BACKFILL_DAYS` (default 0).

#### Scenario: Not enabled
- WHEN `MEDIA_ENABLED_AT` is unset and a voice note is posted in an indexed
  channel
- THEN the message SHALL be stored as today and no media row SHALL be created

#### Scenario: Posted before the moment
- WHEN a message created before `MEDIA_ENABLED_AT` is re-read by backfill or
  reconciliation and `MEDIA_BACKFILL_DAYS` is 0
- THEN no media row SHALL be created for it

#### Scenario: A backfill window
- WHEN `MEDIA_BACKFILL_DAYS` is N and a message created within N days before
  `MEDIA_ENABLED_AT` is re-read
- THEN its attachments SHALL be recorded

#### Scenario: A negative backfill
- WHEN `MEDIA_BACKFILL_DAYS` is negative
- THEN the process SHALL fail at startup

### Requirement: Only allowlisted attachments on the Discord CDN are recorded

The system SHALL record an attachment only when its declared type, without
parameters and lowercased, is one of `audio/ogg`, `audio/mpeg`, `audio/mp4`,
`audio/wav`, `audio/webm`, `image/png`, `image/jpeg` or `image/webp`, and its
URL is https on `cdn.discordapp.com` or `media.discordapp.net`. Audio on a
message carrying Discord's `IS_VOICE_MESSAGE` flag SHALL be recorded as
`voice`, other audio as `audio`, and images as `image`.

#### Scenario: A voice note
- WHEN a message flagged `IS_VOICE_MESSAGE` carries an `audio/ogg` attachment
- THEN one row of kind `voice` SHALL be recorded with its declared type, size,
  filename, declared duration and URL

#### Scenario: Another type
- WHEN an attachment is declared `image/gif`, a video, a document or any type
  outside the allowlist
- THEN no row SHALL be recorded for it

#### Scenario: A URL off the Discord CDN
- WHEN an allowlisted attachment's URL names any other host, or plain http
- THEN no row SHALL be recorded for it

### Requirement: Capture records metadata only

Recording media SHALL NOT download an attachment, contact any host or call any
model, and SHALL NOT store the attachment's bytes.

#### Scenario: A voice note is recorded
- WHEN a voice note in an indexed channel is captured
- THEN exactly one row with status `pending` and zero attempts SHALL exist for
  it
- AND no request SHALL have been made to the Discord CDN or any model

### Requirement: Media is recorded only for stored messages

A media row SHALL reference a stored message and SHALL NOT exist without one.
Messages the system does not store -- in a DM, in a private thread, in a
channel out of indexing scope, by a bot, or by an opted-out person -- SHALL
have no media row.

#### Scenario: Outside an indexed channel
- WHEN a voice note is sent in a DM to the bot, in a private thread, or in a
  channel that is not indexed
- THEN no media row SHALL be created

#### Scenario: An opted-out author
- WHEN a person who opted out posts a voice note in an indexed channel
- THEN neither the message nor a media row SHALL be stored

#### Scenario: Deleted before it was stored
- WHEN a message's deletion is recorded before the message itself is written
- THEN no media row SHALL be created when it is

### Requirement: Re-reading a message refreshes and never resets its media

When a message is written again, the system SHALL replace the URL of each of
its media rows that is still pending, SHALL NOT change any row's status or
attempts, and SHALL delete the rows of attachments the message no longer
carries.

#### Scenario: A fresh CDN URL
- WHEN a message with a pending media row is re-read with a new signed URL
- THEN the row SHALL hold the new URL, with its status and attempts unchanged

#### Scenario: A row already finished with
- WHEN a message whose media row is failed, done, withheld or withdrawn is
  re-read
- THEN the row SHALL be left unchanged

#### Scenario: An edit removes an attachment
- WHEN a message is edited so that it no longer carries an attachment
- THEN that attachment's row SHALL be deleted and the others kept

### Requirement: Removing a message removes or withdraws its media

Deleting a message SHALL, in the same transaction, set each of its media rows
to `withdrawn` and clear its derived text and URL. Retention, opt-out and a
channel leaving indexing scope SHALL delete the media rows of every message
they delete.

#### Scenario: A message is deleted
- WHEN a message with a transcribed voice note is deleted
- THEN its media row SHALL be `withdrawn` with no text and no URL
- AND a later re-read of the message SHALL NOT make it pending again

#### Scenario: Retention, opt-out, channel purge
- WHEN retention removes a message, a person opts out, or a channel leaves
  scope and is purged
- THEN the media rows of the removed messages SHALL be gone

### Requirement: Processing media is a separate, off-by-default decision per kind

The system SHALL NOT download, transcribe or describe any recorded media unless
the switch for its kind (audio, images) is on. When on, it SHALL fetch only
from the Discord CDN through the process's HTTP transport, SHALL send only to
the configured media endpoint, SHALL stop within the kind's hard monthly cap
(1,500 audio minutes a month by default, shared with voice questions), and
SHALL skip any row whose message is deleted, whose channel has left scope, or
whose author has opted out globally or from media processing.

#### Scenario: Switched off
- WHEN pending audio rows exist and the audio switch is off
- THEN nothing SHALL be downloaded or sent

#### Scenario: Over the monthly cap
- WHEN the month's audio minutes are spent
- THEN pending rows SHALL stay pending and no transcription SHALL be requested

#### Scenario: A person opted out of media processing
- WHEN a person has refused media processing and their voice note is pending
- THEN it SHALL NOT be downloaded or transcribed, while their typed messages
  stay indexed

### Requirement: Derived text is treated as the message's own text

Text derived from a voice note or image SHALL be stored only after contact
details are withheld and secrets redacted, SHALL be retrievable only by those
who may read the message's channel, and SHALL be cited as the message.

#### Scenario: Contact details spoken
- WHEN a transcript states the author's own phone number, email, address or
  birth date
- THEN the row SHALL be `withheld` and no text stored

#### Scenario: A secret in a screenshot
- WHEN visible text contains a private key, seed phrase or API key
- THEN the stored text SHALL carry `[redacted]` in its place and the row SHALL
  record how many redactions were made, never what they were
