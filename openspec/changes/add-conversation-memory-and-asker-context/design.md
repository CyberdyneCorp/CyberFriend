## Context

The conversation store exists and is reachable from nothing that matters: it
records questions keyed by channel, in memory, and no reasoning stage reads
them. The asker's roles are resolved for permission checks and then dropped
before the prompt. Both inputs the model needs to hold a conversation are
already computed and thrown away.

## Decisions

### Only the asker's own profile

The model is told the asker's display name, nickname and role names. It is not
told anyone else's, and there is no path to assemble them.

The line matters because the two uses look similar and are not. "What did I
say about the migration" needs to know who "I" is; that is the person
consenting to their own context being used. "What do you know about João" asks
the assistant to profile a colleague who is not in the conversation and has not
agreed to anything. Answering from messages João wrote in channels the asker can
read is fine -- that is ordinary retrieval with citations. Compiling his roles,
join date and activity into a description is not, and it is excluded rather
than left to a prompt to decline.

### Keyed by person and location

A conversation is identified by (person, location), where location is the DM or
channel. The existing store keyed by channel alone, which would have fed one
person's questions into another's follow-up the moment history reached a
prompt. A DM conversation never informs a channel answer, because a DM answer
was scoped to one person and a channel answer is scoped to everyone present.

### Every turn carries the channels it drew on

A remembered answer quotes content the person could read when it was given.
Replaying it after their access is revoked would disclose content they can no
longer see -- a permission change silently bypassed by memory. So each turn
stores the channel ids of its citations, and before use the turn is dropped if
any of them is outside the person's current readable set.

This is a cheap set check, not a re-fetch, and it fails closed: a turn whose
provenance cannot be established is not used.

### Summaries carry the union, and fail as a whole

A summary merges turns and loses per-turn provenance, so it records the union of
their channels. If any one of them is no longer readable the whole summary is
discarded rather than edited. Rewriting a summary to remove one channel's
contribution would require the model to know which sentences came from where,
and guessing wrong leaks exactly what the check exists to stop. Losing a summary
costs some context; the next question rebuilds it.

### Memory interprets; it never grounds

Remembered turns go into the prompt fenced as data, used to resolve what a
follow-up means -- "and last month?" -- and never cited. The answer is grounded
in fresh retrieval under the person's current access. Otherwise a remembered
answer becomes a way to repeat a claim whose sources have since been deleted or
withdrawn.

### Retention, forgetting and opt-out ship with it

This is a new record of what each person asked, which is personal data
separate from the corpus. A forget command, a retention window, and the
existing opt-out are part of this change rather than a follow-up, because a
memory feature that can only be switched off later is one that collected data
nobody agreed to in the meantime.

## Risks / Trade-offs

- **Summaries are model-written** and can be wrong. They are context, not
  evidence, so an error misreads a follow-up rather than asserting a false fact
  -- but a misread follow-up is still a worse answer.
- **Dropping a summary loses context** whenever a single channel it touched
  becomes unreadable. Conservative by design; noticeable in servers where
  permissions change often.
- **Nicknames are attacker-controlled text** that now reaches a prompt. Fenced
  as data like everything else, but it is a new injection surface.
- **Storage grows with use.** Bounded by retention and by summarisation, and
  visible in the admin console's counts.
