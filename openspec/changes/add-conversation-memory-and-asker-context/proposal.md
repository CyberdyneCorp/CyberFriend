## Why

The assistant cannot hold a conversation. A follow-up like "and last month?"
arrives with no context, because the conversation store records the last few
questions and no reasoning stage ever reads them. The store is in memory, so
every restart wipes it; it keeps the person's questions but not the answers;
and it is keyed by channel, so in a shared channel one person's follow-up
would carry somebody else's questions.

The assistant also does not know who it is talking to. It resolves the asker's
roles to decide what they may read, and then passes none of that to the model
-- so it cannot use their name, cannot resolve "my team", and cannot tell who
"I" is in "what did I say about the migration".

## What Changes

- Pass the asker's own Discord profile -- display name, server nickname and
  role names -- into the prompt, as data.
- Keep a conversation per person and per location, including the assistant's
  answers, in the database so it survives restarts.
- Summarise a conversation once it grows past a bound, rather than truncating
  it.
- Use that history when answering, so follow-ups resolve.
- Re-check every remembered turn against what the person can read NOW before
  it is used, and discard what they no longer may.
- Give people control: a command to forget their conversation, a retention
  window, and the existing per-person opt-out applied to memory.

Non-goals:

- **Profiles of other people.** Asking "what do you know about João" must not
  assemble roles, activity and messages into a picture of a colleague. That is
  a surveillance feature, not a memory feature, and it is out of scope.
- Memory shared between people. A conversation belongs to the person having
  it.
- Long-term preference learning. This remembers a conversation; it does not
  build a model of a person.

## Capabilities

### New Capabilities

- `asker-context`: what the assistant is told about the person asking, and
  what it is never told.
- `conversation-memory`: storing, summarising, re-checking and forgetting a
  person's conversation with the assistant.

### Modified Capabilities

None directly. `agent-reasoning` gains an input, but its grounding rule is
unchanged: remembered turns are context for interpreting a question, never
evidence for answering it.

## Impact

- **A new record of what each person asked the assistant.** That is personal
  data in its own right, separate from the corpus. It needs retention,
  deletion on request, and the existing opt-out -- all of which this change
  includes, not defers.
- **A new way to get around a permission change.** A remembered answer quotes
  channels the person could read when it was given. If their access is later
  revoked, replaying that answer would disclose content they can no longer
  see. Every remembered turn carries the channels it drew on, and is dropped
  when any of them is no longer readable.
- **Prompt injection through memory.** A remembered question is text the
  person typed, and a remembered answer quoted retrieved content. Both are
  fenced as data.
