## ADDED Requirements

### Requirement: Outbound queries carry no platform identifier or phone number

A query sent to the web, Wikipedia or an MCP server SHALL NOT contain a
platform identifier of any enabled platform (a Discord mention or snowflake,
a Slack member, channel or user-group id, a WhatsApp BSUID) or a phone number
in E.164 form. The phone-number check is new behaviour on Discord and SHALL
ship in its own pull request, with the snapshot differences it causes listed
there as an intended change.

#### Scenario: A Discord mention in a web query (regression)
- WHEN a Discord person's question would send a `<@1234…>` mention to web
  search
- THEN the query SHALL be refused exactly as before this change

#### Scenario: A phone number in a web query
- WHEN a question would send `+5511987654321` to web search
- THEN the query SHALL NOT leave the deployment

#### Scenario: A Slack id in a web query
- WHEN a question would send `U0123ABC` or `<!subteam^S0123>` to an MCP server
- THEN the query SHALL NOT leave the deployment
