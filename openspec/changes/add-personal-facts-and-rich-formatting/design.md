## Decisions

### A closed set of facts

Preferred name, email and language. Each has an immediate use -- addressing,
reaching, and replying -- and a closed set keeps this from becoming a notes
store. Anything the person writes into a free-form memory reaches every later
prompt, which makes an open-ended "remember this" a durable injection point.

### Set by the person, to the assistant, about themselves

A fact is stored only from the asker's own message addressed to the assistant.
It is never extracted from channel messages, and never from one person's
statement about another. The alternative -- collecting what people say about
each other -- is the colleague-profiling this project excludes elsewhere.

### Separate from conversation memory

Conversation memory expires, is kept per location, and only helps interpret a
follow-up. Facts persist until deleted and belong to the person rather than to
a channel. They share `/forget` and opt-out, and live in their own table.

### Email only in direct messages

A channel reply is read by everyone in the channel. The preferred name was
chosen for being addressed and may be used anywhere; an email address is shown
only in a direct message to its owner, and nobody else can retrieve it.

### Markdown with an allowlist of links

The model is asked to format with Discord markdown, and the renderer enforces
what the model cannot be trusted to: masked links are kept only for citations
the assistant generated, everything else is unmasked to its bare address,
excerpts are escaped, and messages split outside code blocks. Mentions remain
suppressed at the client, as today.

## Risks

- Intent detection for "call me" and "my email is" will miss some phrasings.
  A miss means the fact is not stored and the person is told what can be
  remembered, which is recoverable. A false positive could overwrite a stored
  fact, so every stored fact is confirmed back to the person.
- Replying in a preferred language changes answers for questions asked in
  another language. The person chose it, and can change it.
- Formatting guidance costs prompt tokens on every answer.
