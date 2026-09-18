## Why

Everything the assistant does waits for a question. Two things people want are
the ones it cannot do while waiting.

"What did I miss?" — coming back to a busy channel means reading it. The corpus,
the windows and the viewer scoping needed to summarise it already exist; nothing
offers it.

"Did anyone ask me for something?" — obligations are extracted and can be closed,
and the only way to find out one exists is to ask. A feature nobody is told about
is one nobody uses.

## What Changes

- **Catch up.** A command summarises what happened in a channel over a period,
  from the messages the asker may read, with citations.
- **Notifications.** When an obligation addressed to a person is extracted, the
  assistant sends them a direct message, batched and rate-limited, and only if
  they have not turned it off.

Non-goals:

- **Messaging people who never interacted with the assistant.** A notification
  is only ever sent to the person an obligation was addressed to.
- **Broadcasts.** Nothing is posted into a channel unprompted.
- **Read receipts.** No bot can see what a person has read. A period is either
  stated by the asker or derived from their own last message.

## Capabilities

### New Capabilities

- `catch-up`: summarising a period of a channel for one viewer.
- `notifications`: the messages the assistant sends without being asked, who
  receives them, and how someone stops them.

### Modified Capabilities

None.

## Impact

- **The assistant sends messages nobody asked for.** That is the point and the
  risk. Every notification is addressed to the person the obligation names, is
  batched so a busy day is one message, is rate-limited, and can be turned off
  in one command. Nothing is sent to a person who has opted out.
- **A summary is a new surface for disclosure.** It is built from the same
  viewer-scoped retrieval as any answer, and is re-checked at send time: a
  notification about a channel someone can no longer read is not sent.
- **Notifications cost model calls** on ingest rather than on a question.
  Extraction already runs there; summarising a batch adds to it.
