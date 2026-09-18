## Decisions

### No read receipts, so the period is explicit

No bot can see what a person has read. "What did I miss" therefore means a period
the asker states, or a default the answer names. Guessing from their last message
in the channel is tempting and wrong: someone who reads without posting would be
told they missed a month.

### Notifications are earned, not broadcast

The assistant already answers when asked. Sending a message nobody asked for is a
different relationship, so it is bounded on every side: only the person an
obligation names, only for obligations naming an individual, batched into one
message, rate-limited, stoppable in one command, never to someone who opted out,
and never at all if they cannot still read the channel it came from.

### Permission is re-checked at send time

Extraction and delivery are separated by a queue, and access can change in
between. The check is repeated immediately before sending, for the same reason
conversation memory re-checks a remembered turn: a permission change must not be
outrun by a delivery.

### Summaries reuse retrieval rather than reading the channel

A summary is built from the same viewer-scoped retrieval every answer uses, so a
channel the asker cannot read is not summarised, and a summary asked for in a
public channel is bounded by that channel's audience. Reading the messages
directly would be a second path to content with its own permission rules.

## Risks

- **Notifications are the feature most likely to annoy.** The batching window
  and rate limit are guesses until people use it; both are configurable.
- **A summary is model-written and can mislead** by omission. It cites what it
  drew on, so a reader can check.
- **Extraction already costs model calls on ingest**; batching summaries adds to
  that. It is bounded by the same per-pass limits.
