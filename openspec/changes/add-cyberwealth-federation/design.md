## Context

Federation opens each configured server with `default_session_factory`, which
builds `mcp.client.Client(target)`. For a URL the SDK uses its own
`httpx2.AsyncClient` with no headers. CyberWealth accepts exactly one bearer
per request. For a service principal that bearer is a CyberdyneAuth
`client_credentials` token with `aud` including `cyberwealth` and scope
`cyberwealth:intel.read`. CyberdyneAuth's token endpoint is
`POST {issuer}/api/v1/auth/oauth2/token` (advertised by discovery). It
accepts `audience` and `scope` form fields and client_secret_basic or
client_secret_post, and returns `{access_token, token_type: "Bearer",
expires_in}`.

## Decisions

### One token source per server, owned by that server's HTTP client

`ServiceTokenSource` holds one credential. It resolves the token endpoint
from `{issuer}/.well-known/openid-configuration` once, checks that discovery
names the configured issuer, and posts `grant_type=client_credentials` with
`audience` and `scope` when set. The token is cached until `expires_in` minus
a 60 s margin (never below zero; 300 s when the issuer omits `expires_in`).
Fetches are serialised by a lock, so concurrent calls after expiry cause one
request.

`ServiceBearerAuth` is an `httpx2.Auth`. It sets the header on each request
and, on a `401`, drops that token, fetches a new one and retries the request
once. The auth object is attached to an `httpx2.AsyncClient` built for that
one server. It is never set as a default header or on a shared client, so
the token cannot reach another server, a redirect target outside the origin
(the SDK does not follow those), or the token endpoint.

Token fetches go through `httpx` with the process's injected transport
(`Edges.http_transport`), like the admin sign-in provider, so tests and the
e2e harness can fake the issuer. Neither the secret nor a token is logged.
`repr` hides the secret, and a failed grant is logged by its OAuth `error`
code only.

A failed fetch raises inside the MCP request. At startup that makes the
server unreachable, which already degrades to "the rest still works". Mid-run
it marks the server lost for that run's answer, as any transport failure does.

### Configuration from the environment

The variable names are chosen per server, so they cannot be pydantic fields.
`service_credentials(server_names, environ)` reads every `FEDERATION_AUTH_*`
key. It matches the suffix (`_CLIENT_SECRET`, `_CLIENT_ID`, `_ISSUER`,
`_AUDIENCE`, `_SCOPE`) and maps the rest to a configured server. These are
refused:

- a key naming no configured server;
- a server with some but not all of issuer, client id and secret;
- an issuer or a server target that is not `https://`, except for localhost.
  A bearer on plain HTTP is a bearer anyone on the path can replay.

`environ` is `os.environ` over the `.env` file, the same sources `Settings`
reads. The variables reach the bot only because `docker-compose.yml` declares
them on its service (`tests/unit/test_compose_env.py` holds every operator
setting to that): a variable set in the platform but not declared there never
reaches the process, and the server is opened without a bearer. So each new
credentialled server needs its five `FEDERATION_AUTH_<NAME>_*` lines added to
the bot service. Declaring them for the bot alone is not a secrecy boundary,
and nothing here assumes the secret is absent from other processes; only the
bot process builds federation, so only it uses the secret.

A credential that cannot be honoured (below) drops the remote MCP servers
for that boot and logs `composition.federation.service_auth_misconfigured`.
The local web, market and wallet tools never carry a credential and stay
registered.

### Where the personal gate sits

Two independent checks:

1. **By name, at authorization.** `ToolPermit.personal` is set at
   registration from the tool name (`my_` prefix). `Authorizer.authorize`
   refuses `PERSONAL_OUTSIDE_DIRECT` when `request.direct` is false. It runs
   after the registration and offer checks, so the refusal reason stays
   specific, and before credentials, so nothing is sent.
2. **By result, after the call.** `ToolResult.personal` is true when the
   text starts with `[personal]` or the structured content has
   `visibility == "personal"`. `GuardedInvoker` withholds such a result when
   the request is not direct. The run gets a `PERSONAL_WITHHELD` failure with
   no text, and the audit records it as refused. This catches a server that
   labels a result personal from a tool not named `my_`.

`direct` is `audience.mode is DIRECT_MESSAGE`, not `is_private`. An ephemeral
reply in a guild channel is seen only by the asker, but the partner guide says
DMs only, and ephemeral replies are remembered under the channel's location.

The router still offers `my_*` in channel runs. `offer` and `invoke` both
route from the question alone, and they must match. Filtering by audience in
one and not the other would turn every channel call into `NOT_OFFERED`.

### Memory and labelling

`answer_provenance` already refuses to remember a turn that rests on a
source outside the corpus and outside `PUBLIC_SOURCE_SYSTEMS`. CyberWealth
evidence is filed under its server name, so no turn resting on it is
remembered, in a channel or a DM. `intel_*` stays out of
`PUBLIC_SOURCE_SYSTEMS` on purpose. Regression tests pin both, so a later
change that makes CyberWealth "public" has to break a named test.

## Risks

- `my_*` may be offered in a channel run and then refused, so the answer
  says less than it could. Redirecting the asker to a DM is left for the
  connected-app-key change, when `my_*` becomes callable at all.
- The service principal's per-minute limit (120) is shared by every asker.
