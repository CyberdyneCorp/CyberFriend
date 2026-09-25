## Context

The admin API is a single Starlette process (`replicas: 1`) that serves the
console bundle and `/api/*` from one origin with no CORS. Every `/api` request
passes `AdminAuthMiddleware`, which accepts exactly one `Authorization: Bearer
cfa_...` header, returns one byte-identical 401 for every failure, and binds an
`Operator(name)` for handlers and the audit log.

CyberdyneAuth's contract (from its team):

- issuer `https://auth.backend.coolify.cyberdynecorp.ai`, discovery at
  `/.well-known/openid-configuration`;
- authorization code + PKCE S256; a confidential BFF is recommended;
- access tokens verified locally via JWKS: RS256, `iss` from discovery, `exp`,
  `type == "access"`, `aud == "cyberfriend"`;
- roles claim is a list of `"<client_id>:<role>"`; an absent claim is unknown
  and must be denied;
- key users on `sub`; email via userinfo;
- access 15 minutes; refresh 30 days, rotating, with reuse detection;
- logout = revoke our session, then `end_session` with `id_token_hint`;
- introspection available with client_credentials; no SDK.

## Goals / Non-Goals

**Goals:**

- No token reaches the browser.
- One place decides which role each route needs, and it fails closed.
- Every change stays attributable to a person.
- Scripts that use `cfa_` tokens keep working.

**Non-Goals:**

- Multiple replicas (the refresh lock is in-process; see Risks).
- A user-facing area.

## Decisions

### BFF with an opaque cookie, not tokens in the SPA

The SPA never runs OIDC. `GET /auth/login` generates `state`, `nonce` and a
PKCE verifier, stores them in a short-lived (10 minute) server-side login
record keyed by a hash of `state`, and 302s to the authorization endpoint with
`redirect_uri = <public base>/auth/callback`. The callback checks `state` (single
use), exchanges the code with the client secret and verifier, verifies the
access token and the id token (`nonce`), fetches userinfo for the email,
creates a session, sets the cookie and 302s to `./#/status`. The code and state
never reach the SPA, and `<meta name="referrer" content="no-referrer">` keeps
them out of Referer headers.

Cookie: `__Host-cf_admin`, `HttpOnly; Secure; SameSite=Strict; Path=/`, value
= 32 random bytes. The console's storage guard still forbids
`document.cookie`, and JavaScript cannot read the cookie anyway.

Alternative considered: a public SPA client holding tokens in memory. Rejected:
the CyberdyneAuth team recommends a BFF, refresh tokens would have to live in
the browser, and the project's rule is that the browser holds no credential in
storage.

### Server-side session table

`admin_session(id_hash PK, sub, email, roles text[], access_token_enc,
refresh_token_enc, id_token_enc, access_expires_at, created_at, last_seen_at,
expires_at, revoked_at)`:

- The id is stored as its sha256, like `admin_token`.
- The tokens are encrypted at rest with AES-GCM under `ADMIN_SESSION_KEY`. The
  key is never logged, and holding the database alone does not yield usable
  tokens.
- The session ends when the refresh token expires (30 days), on logout, when
  refresh fails, or after 12 hours without activity, whichever comes first.

### Verification rules

Every request with a session re-checks the stored access token's signature,
`iss`, `exp`, `type` and `aud` before it is used. The roles come from the
verified access token, never from userinfo or the client. JWKS is cached and
refetched once when an unknown `kid` is seen, never more than once a minute.
Discovery is fetched at startup and cached. When the issuer is not configured,
`/auth/login` returns 404 and only bearer tokens work, so a deploy without OIDC
behaves exactly as today.

### Refresh, serialised per session

Within 60 seconds of `access_expires_at`, the middleware refreshes before
handling the request. Because refresh tokens rotate with reuse detection, two
concurrent refreshes of one session would present the same refresh token twice
and CyberdyneAuth would revoke the whole token family. An `asyncio.Lock` per
session id serialises it: the second request waits and then reads the new
token. A refresh that fails ends the session (401, and the console sends the
person back to login). Roles are re-read from each new access token, so a role
withdrawn in CyberdyneAuth takes effect within 15 minutes.

### Roles and the route table

```
Role = operator < admin
Principal(subject, display, roles, via = "oidc" | "token")
```

- `<client_id>:admin` gives admin, `<client_id>:operator` gives operator, and
  admin implies operator.
- An absent roles claim, or one with neither role, yields no console role. The
  login still completes, but the session is not created and the person sees a
  fixed "no access" page. The console does not reveal that the account exists.
- A `cfa_` bearer maps to admin, `via = "token"`.

`server.py` keeps one table beside `api_routes()` mapping `(method, path)` to a
minimum role. Every GET is operator unless the table says otherwise; every
other method is admin. A route missing from the table is refused. A unit test
walks every mounted route and asserts that each non-GET route requires admin.
Below the role: 403 `{"error": "requires admin"}`. The 401 stays byte-identical
for every authentication failure.

### Exactly one credential

A request with a bearer header and a session cookie is refused with 401. The
middleware does not choose between them, following the existing "ambiguity is
refused" rule (auth.py 342-356).

### CSRF and headers

Cookie auth brings CSRF, which bearer tokens did not have:

- SameSite=Strict.
- Every non-GET `/api` and `/auth/logout` request must carry
  `X-CyberFriend-Console: 1`. A cross-site form cannot set it, and a cross-site
  `fetch` with it needs a CORS preflight, which the server never grants.
- `Origin`, when present, must equal the configured public origin.

Every response carries
`Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self' <issuer>`,
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.

These apply to bearer requests too. They cost scripts nothing (scripts send no
Origin) and keep one code path.

### Audit attribution

`Operator.name` must match `[a-z0-9][a-z0-9._-]{0,63}`, and emails and OIDC
subjects do not. An OIDC change is recorded with actor `oidc:<sub>`. The `:`
namespace is already reserved for actors only the server constructs
(`cli:shell`). A new nullable `config_audit.operator_display` column holds the
email, for display. `ALTER TABLE ... ADD COLUMN` does not touch the append-only
trigger, and a test asserts that UPDATE and DELETE are still refused. Token
actors are recorded exactly as today.

### Logout

`POST /auth/logout` (CSRF header required) marks the session revoked, revokes
the refresh token at CyberdyneAuth's revocation endpoint (best effort), clears
the cookie and returns the `end_session` URL with `id_token_hint` and
`post_logout_redirect_uri`. The console then navigates there.

### Outbound HTTP through an injected transport

`entrypoints/admin.build()` gains a `transport: httpx.AsyncBaseTransport | None`
parameter, used for discovery, JWKS, token, userinfo and revocation, mirroring
`Edges.http_transport`. The e2e harness gains an admin process with a FakeOIDC
issuer (a generated RSA key, served JWKS) behind that transport.

### Posture change, recorded

`entrypoints/admin.py` says the console holds database credentials only. After
this change it also holds the OIDC client secret and the session-encryption
key. Neither can reach the corpus or the bot's accounts. `FORBIDDEN_CREDENTIALS`
(Discord token, LLM key, SerpAPI key) is unchanged, and the new secrets are
added to the secret set that is never recorded or displayed.

## Risks / Trade-offs

- [One route missing its role check lets an operator purge a person] -> One
  table, closed by default for non-GET, plus the walk-all-routes test.
- [Refresh reuse detection revokes the family under concurrency] -> Per-session
  lock. This holds while `replicas: 1`; scaling out would need
  `SELECT ... FOR UPDATE` on the session row. That is a task, not an
  assumption.
- [A leaked `cfa_` token is admin with no expiry] -> Unchanged from today.
  Retirement is an open question with a date to set.
- [CyberdyneAuth unreachable] -> Login fails and existing sessions keep working
  until their access token needs a refresh. Bearer tokens keep working.
- [Role claim semantics change on the provider side] -> Absent or unfamiliar
  claims deny. Tests pin that behaviour.

## Migration Plan

1. Deploy with `ADMIN_OIDC_ISSUER` unset: behaviour is identical to today.
2. Register the `cyberfriend` client (redirect URI `/auth/callback`,
   post-logout URI) and assign roles in CyberdyneAuth.
3. Set the issuer, client id, secret, session key and public URL. Admins sign in
   with CyberdyneAuth, and scripts keep their tokens.
4. Rollback: unset the issuer. Sessions stop being accepted and tokens still
   work.
