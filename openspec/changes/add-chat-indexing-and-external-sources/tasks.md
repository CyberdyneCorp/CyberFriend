## 1. Live scope

- [ ] 1.1 Bot and ingest read indexing scope from runtime configuration, falling back to the environment
- [ ] 1.2 Refresh on a bounded period; keep current scope when a refresh fails
- [ ] 1.3 Ingest starts capturing and backfilling a newly added channel without a restart
- [ ] 1.4 The ACL resolver uses the current scope, not the startup value
- [ ] 1.5 Test: a channel added to stored scope is captured without a restart
- [ ] 1.6 Test: a failed refresh keeps the current scope

## 2. Index from Discord

- [ ] 2.1 `/index` and `/unindex` commands taking a channel
- [ ] 2.2 Require Manage Channels on the target channel, resolved from live guild state
- [ ] 2.3 Refuse, naming the missing permission, when the bot cannot read the channel
- [ ] 2.4 Post a notice in a newly indexed channel
- [ ] 2.5 Removal purges the channel's content through the existing purge path
- [ ] 2.6 Record every attempt in the change record, allowed or refused
- [ ] 2.7 Test: a person without Manage Channels is refused and scope is unchanged
- [ ] 2.8 Test: authority claimed in the message text has no effect

## 3. External sources

- [ ] 3.1 An explicit "search the web" request skips the corpus
- [ ] 3.2 Fall back to external sources when the critic judges evidence insufficient, not only on abstention
- [ ] 3.3 Web citations carry a working link
- [ ] 3.4 Refuse MCP server changes requested from chat, pointing to the admin console
- [ ] 3.5 Test: a corpus answer that answers the question never consults the web

## 4. MCP in production

- [ ] 4.1 Configure Context7 read-only in FEDERATION_SERVERS and FEDERATION_TOOL_ALLOWLIST
- [ ] 4.2 Verify live: a library documentation question is answered through Context7 with a citation

## 5. Market data

- [ ] 5.1 Provider for BTC and ETH via CoinGecko, with quote timestamp
- [ ] 5.2 Provider for currency conversion via Frankfurter at `api.frankfurter.dev/v1`, stated as a daily reference rate
- [ ] 5.3 Provider for the S&P 500 via SerpApi Google Finance, reporting retrieval time
- [ ] 5.4 Validate instruments against a fixed list and currencies against ISO 4217 before any request
- [ ] 5.5 Egress guard checks closed-set membership for these tools instead of word rooting
- [ ] 5.6 Route current-price questions to market data, never the corpus
- [ ] 5.7 Cache results briefly; an unavailable provider reports unavailability, never a stale figure
- [ ] 5.8 Register the tools read-only through the existing federation path
- [ ] 5.9 Test: "ETH" resolves and is not refused by the guard
- [ ] 5.10 Test: free text in a currency argument is refused before any request
- [ ] 5.11 Test: a price quoted in a channel is never returned as the current price
- [ ] 5.12 Test: asked whether to buy, the answer makes no recommendation

## 6. Verification

- [ ] 6.1 Live: index a channel from Discord, post in it, and retrieve the message without a redeploy
- [ ] 6.2 Live: unindex it, and confirm its messages are no longer returned
- [ ] 6.3 Live: ask a general question and get a labelled, linked web answer
- [ ] 6.4 Live: ask for BTC, the S&P 500 and a USD to BRL conversion, each with its age stated
