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

- No access or refresh token reaches the browser. The id token reaches it
  only as the `id_token_hint` of the end-session URL at sign-out, after our
  session is revoked; RP-initiated logout needs it there.
- One place decides which role each route needs, and it fails closed.
- Every change stays attributable to a person.
- Scripts that use `cfa_` tokens keep working.

**Non-Goals:**

- Multiple replicas (the refresh lock is in-process; see Risks).
- A user-facing area.

## Decisions

### BFF with an opaque cookie, not tokens in the SPA

The SPA never runs OIDC. `GET /auth/login` generates `state`, `nonce`, a PKCE
verifier and a browser-binding value `b` (32 random bytes). It stores them in a
short-lived (10 minute) server-side login record
`admin_login(state_hash PK, nonce, verifier, binding_hash, purpose, link_code_hash NULL, max_age NULL, expires_at, used_at)`,
sets the pre-auth cookie `__Host-cf_login=b` (`HttpOnly; Secure;
SameSite=Lax; Path=/; Max-Age=600`), and 302s to the authorization endpoint with
`redirect_uri = <public base>/auth/callback`. SameSite=Lax is required here:
the callback is a cross-site top-level navigation from the issuer, and a
Strict cookie would not be sent.

The callback, in order:

1. Looks up the login record by the hash of `state`; it must exist, be
   unexpired and unused. It is marked used in the same transaction.
2. Requires `__Host-cf_login` and `sha256(b) == binding_hash`, then clears the
   cookie. A callback URL replayed into another browser (login CSRF, session
   swapping) fails here.
3. Exchanges the code with the client secret and verifier.
4. Verifies the access token (rules below) and the id token: RS256 via JWKS,
   `iss` from discovery, `aud == client_id`, `exp`, and `nonce` equal to the
   login record's. A missing or different nonce is refused.
5. Fetches userinfo and requires `userinfo.sub == id_token.sub ==
   access_token.sub` (OIDC Core 5.3.2). Email and `email_verified` are read
   only after that check.
6. Decides the role, creates the session, sets the session cookie and 302s to
   `./#/status`.

The code and state never reach the SPA, and
`<meta name="referrer" content="no-referrer">` keeps them out of Referer
headers. The same login record, with `purpose` and `link_code_hash`, is reused
by the user area's `/link` flow and by fresh sign-in (`max_age`).

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
person back to login).

Roles are re-checked on every refresh, from the new access token only:

- roles claim absent: deny. The session is revoked and the request gets the
  standard 401.
- claim present but neither console role: the admin session is revoked and the
  request gets 403 `{"error": "no console access"}`.
- role lowered from admin to operator: the session continues as operator.

A role withdrawn in CyberdyneAuth therefore takes effect within 15 minutes.

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
- A `cfa_` bearer is `via = "token"`. While `ADMIN_OIDC_ISSUER` is unset it is
  admin, exactly as today, so a deploy without OIDC keeps working. Once the
  issuer is set, every `cfa_` token is operator: counts and configuration
  reads only. Admin rights then come only from the CyberdyneAuth role, which
  is named, managed and revocable at the provider.
- Personal content (question text, and any later personal-content route)
  additionally requires `via == "oidc"`. A token principal gets 403 there,
  whatever its role.

`server.py` keeps one table beside `api_routes()` mapping every mounted
`(method, path)` to its access level: `public` (`/auth/*`, `/link`, the static
bundle), `user` (`/me/*`, added by the user area), `operator`, `admin`, or
`admin_oidc`. There is no per-method default. A route with no row is refused
with 403 before its handler runs (deny by default), so forgetting a row can
never expose a route. A unit test enumerates every route the Starlette router
mounts and fails when:

- a route has no row;
- a non-GET route is anything other than `admin` or `admin_oidc`;
- `GET /api/usage/people/{id}/questions` is anything other than `admin_oidc`.

Below the role: 403 `{"error": "requires admin"}`. The 401 stays byte-identical
for every authentication failure.

### A bearer header wins; cookies on a bearer request are ignored

A request with `Authorization: Bearer` is authenticated by the bearer alone.
The middleware strips the `Cookie` header from that request before any handler
or session code sees it: the session is not read, refreshed or touched. A
browser that still holds `__Host-cf_admin` from an earlier sign-in can
therefore use the break-glass token sign-in without being locked out, and the
bearer cannot be combined with a session to gain the other's rights. An
invalid bearer is a 401 even when a valid cookie is present; the cookie is
never used as a fallback.

In the console, `services/http.ts` (cookie mode) sends
`credentials: "same-origin"` and never sets `Authorization`.
`services/breakGlassHttp.ts` (token mode) sends the bearer with
`credentials: "omit"`. The storage guard allows the strings `Authorization`
and `Bearer` only in `services/breakGlassHttp.ts`.

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

Reading personal content is audited too: each question-text read writes a
`config_audit` entry with actor `oidc:<sub>`, the viewer's email in
`operator_display`, and the viewed person looked up server-side (person id and
current display name), plus the window.


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

- [One route missing its role check lets an operator purge a person, or read
  question text] -> One table, deny by default for any route without a row,
  plus the walk-all-routes test.
- [Refresh reuse detection revokes the family under concurrency] -> Per-session
  lock. This holds while `replicas: 1`; scaling out would need
  `SELECT ... FOR UPDATE` on the session row. That is a task, not an
  assumption.
- [A leaked `cfa_` token is admin with no expiry] -> Once OIDC is configured
  tokens are operator only and never read personal content. Admin writes need
  a CyberdyneAuth role.
- [CyberdyneAuth down while an admin write is urgent] -> Break-glass is a
  rollback: unset the issuer and `cfa_` tokens are admin again. The issuer
  alone is the switch, so the other four may stay set (they are ignored with
  a warning); an issuer set without them refuses to start. This is
  recorded in the operations docs.
- [CyberdyneAuth unreachable] -> Login fails and existing sessions keep working
  until their access token needs a refresh, in a process that cached discovery
  and the key set before the outage (both are in memory only; after a restart
  during the outage every session request is a 401). Bearer tokens keep
  working.
- [Role claim semantics change on the provider side] -> Absent or unfamiliar
  claims deny. Tests pin that behaviour.

## Migration Plan

1. Deploy with `ADMIN_OIDC_ISSUER` unset: behaviour is identical to today.
2. Register the `cyberfriend` client (redirect URI `/auth/callback`,
   post-logout URI) and assign roles in CyberdyneAuth.
3. Set the issuer, client id, secret, session key and public URL. Admins sign in
   with CyberdyneAuth, and scripts keep their tokens.
   From this point `cfa_` tokens are operator only.
4. Rollback: unset the issuer (the other four may stay). Sessions stop being
   accepted and tokens are admin again.
