# add-cyberwealth-federation

Connect CyberFriend to CyberWealth's MCP server as a federated tool source.
The server takes a CyberdyneAuth `client_credentials` bearer, so federation
gains per-server service authentication: a token fetched and refreshed from
the issuer and sent only on that server's MCP requests. CyberWealth's
personal tools (`my_*`) and personal results (`[personal]`,
`visibility: personal`) are held to direct messages, and its public
intelligence tools (`intel_*`) are cited as an external system rather than
treated as a public source.

- `proposal.md`: why, and what changes
- `design.md`: token lifecycle, where the personal gate sits, what is deferred
- `specs/mcp-federation/spec.md`: service authentication and personal tools
- `tasks.md`: progress
