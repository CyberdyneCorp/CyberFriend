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

### Market data uses closed vocabularies instead of word rooting

The egress guard admits a query only if every word came from the asker. That
fits a free-text search and breaks a structured lookup: someone asks about
"ETH", CoinGecko needs "ethereum", and the guard trims the unrooted word to
nothing and refuses the call.

Rooting exists to stop free text leaving. An argument that must be one of a
fixed set of instruments, or a valid ISO 4217 code, cannot carry free text, so
for these tools the guard checks membership in the closed set instead. It is
enforced before any request is made, and anything outside the set is refused
rather than trimmed.

### Sources

- Crypto: CoinGecko, no key, with a quote timestamp.
- Exchange rates: Frankfurter over ECB reference rates, no key, published once
  per business day. They are stated as daily reference rates with their date,
  not as live rates.
- S&P 500: Google Finance through the existing SerpApi key. It returns no quote
  time, so the retrieval time is reported and labelled as such. It spends
  SerpApi quota, so results are cached briefly.

### Price questions skip the corpus

A channel message quoting a price is a record of what someone said, not a price.
Routing price questions straight to market data prevents a stale figure being
presented as current.

## Risks

- Free tiers rate-limit. A short cache absorbs bursts; a provider outage degrades
  to saying the figure is unavailable, never to an older figure.
- Reference rates lag the market. Stating that plainly is the mitigation.

- A person with Manage Channels can archive a channel its members would rather
  keep ephemeral. The notice makes that visible; it does not prevent it.
- Falling back on weak evidence sends more queries outside. They stay bounded to
  the asker's words.
- Refresh is periodic, so there is a window where a removed channel is still
  captured. Its content is purged on removal, so anything captured in the window
  is withdrawn too.
