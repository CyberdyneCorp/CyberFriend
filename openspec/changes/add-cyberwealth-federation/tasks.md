## 1. Service authentication

- [x] 1.1 `service_credentials(server_names, environ)`: parse `FEDERATION_AUTH_<NAME>_*`; refuse unknown servers, half-written credentials, non-HTTPS issuers and targets
- [x] 1.2 `ServiceTokenSource`: discovery, `client_credentials` grant with audience and scope, cache until expiry minus margin, one fetch at a time, secret and token never logged
- [x] 1.3 `ServiceBearerAuth` (`httpx2.Auth`): bearer on every request, one retry with a fresh token on `401`
- [x] 1.4 Session factory: an authenticated `httpx2.AsyncClient` per credentialled server, others unchanged; wired in `build_federation`
- [x] 1.6 Deployment: the CyberWealth `FEDERATION_AUTH_*` variables declared on the bot service in `docker-compose.yml`; a refused credential drops only the remote servers, keeping the local tools
- [x] 1.5 Tests: token cached and refreshed; `401` refetches once; the header reaches only its own server; a real MCP server over ASGI sees the bearer; config errors; the process environment wins over `.env`; the compose file passes the credential to the bot

## 2. Personal tools and results

- [x] 2.1 `ToolPermit.personal` from the `my_` prefix; `InvocationRequest.direct` from the audience
- [x] 2.2 `Authorizer` refuses a personal tool outside a DM (`PERSONAL_OUTSIDE_DIRECT`)
- [x] 2.3 `ToolResult.personal` from `[personal]` or `visibility: personal`; `GuardedInvoker` withholds it outside a DM
- [x] 2.4 Tests: `my_*` refused in a channel and allowed in a DM; a personal result withheld in a channel; a turn citing the server is not remembered; the server is not a public source; `intel_*` cited under the server's name

## 3. Docs

- [x] 3.1 README / operations: CyberWealth dev endpoint, the `FEDERATION_AUTH_*` settings, allowlist example, DM-only rule
