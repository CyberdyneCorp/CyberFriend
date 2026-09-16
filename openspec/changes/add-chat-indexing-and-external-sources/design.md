## Decisions

### Manage Channels, resolved live

The person who may make a channel an archive is whoever already administers it
in Discord. Using Discord's own permission avoids a second list of who may do
this that drifts from the server, and resolving it from live guild state rather
than from the message means a claim of authority in the text is ignored.

### The channel is told by the bot

Indexing is opt-in because a permanent searchable archive is a decision people
should know about. A notice posted in the channel at the moment it is indexed is
the only disclosure every member is certain to see.

### Removal purges

De-indexing reuses the purge path retention and opt-out already use. Stopping
capture while leaving history searchable would make "remove this channel" mean
something different from what the person asked for.

### Weak evidence, not only empty evidence, falls back

Escalation currently fires only on abstention. The critic already judges
whether evidence answers the question; that judgement, not an empty result set,
is what should send a question outside.

### Live scope finishes runtime configuration

Scope is the first setting read live. It goes through the existing runtime
configuration store, keeps current values on a failed refresh, and is the
mechanism the admin console's channel screen already assumes.

## Risks

- A person with Manage Channels can archive a channel its members would rather
  keep ephemeral. The notice makes that visible; it does not prevent it.
- Falling back on weak evidence sends more queries outside. They stay bounded to
  the asker's words.
- Refresh is periodic, so there is a window where a removed channel is still
  captured. Its content is purged on removal, so anything captured in the window
  is withdrawn too.
