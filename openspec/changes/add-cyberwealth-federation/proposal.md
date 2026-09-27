## Why

CyberWealth exposes financial intelligence over MCP (streamable HTTP) and
wants CyberFriend as a partner agent (CyberWealth
`docs/partners/mcp-integration.md`). Its MCP server refuses any request
without `Authorization: Bearer <credential>`, and federation today can only
open an unauthenticated URL. The partner guide also sets rules CyberFriend
does not yet follow: `my_*` tools and every result that starts with
`[personal]` or carries `visibility: personal` are for the user alone and must
be shown only in direct messages, and `intel_*` results are external content,
not a public source.

## What Changes

- **Per-server service authentication.** A federated server may be given a
  CyberdyneAuth client: `FEDERATION_AUTH_<NAME>_ISSUER`, `_CLIENT_ID`,
  `_CLIENT_SECRET`, and optional `_AUDIENCE` and `_SCOPE`, where `<NAME>` is
  the server name upper-cased with `-` written `_`. CyberFriend fetches a
  `client_credentials` token from the issuer's token endpoint (found by OIDC
  discovery), caches it until shortly before it expires, fetches a new one
  when the server answers `401`, and sends it as `Authorization: Bearer` on
  that server's MCP requests and on no other request.
- A half-written credential, a credential for a server that is not
  configured, a non-HTTPS issuer, or a credential for a server whose target is
  not HTTPS is a configuration error at startup, reported like every other
  federation misconfiguration.
- **Personal tools are DM-only.** A federated tool whose name starts with
  `my_` is refused at invocation unless the answer goes to the asker's direct
  messages. A result that starts with `[personal]` or carries
  `visibility: personal` is withheld from any answer that is not a direct
  message, whichever tool produced it.
- **Personal results are never remembered in channel memory**, and
  **`intel_*` results stay external**: cited under the server's name and
  never added to the public sources that conversation memory may rest on.
- Deployment docs: CyberWealth dev endpoint and the settings to use.

## Non-goals

- A user's own CyberWealth connected-app key (`cwk_live_...`), stored per
  person and sent only on that person's calls. That is a new secret per person
  and needs its own change (partner guide D10 item 2). Until then `my_*`
  tools are not callable with the service principal, which only sees
  `intel_*`.
- Write tools (`my_register_payment`, ...). They stay behind
  `:enable-mutation` and `FEDERATION_CREDENTIAL_HOLDERS` as today.
- Adding `intel_*` to the public sources memory may keep. The guide leaves
  ownership of public DeFi and price intelligence open.

## Impact

- `adapters/mcp_client`: new `service_auth` module; the session reads
  structured content; permits and results carry `personal`.
- `app/authorization`: a new refusal for personal tools outside a DM;
  `InvocationRequest.direct`.
- `app/reasoning/loop`: sets `direct` from the audience.
- `composition.build_federation`: builds token sources from the environment.
- No migration and no new table.
