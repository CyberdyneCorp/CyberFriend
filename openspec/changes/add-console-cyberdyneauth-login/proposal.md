## Why

Console access today is a static bearer token per operator, issued from a shell
(`python -m chatmemory.admin.issue_token`). That was a deliberate choice when
the console had one kind of user and no personal data (design.md of
`add-admin-console`). Two things change it:

- The console is about to show per-person usage and, to admins only, the text of
  the questions people ask. That needs roles, and an identity that is managed,
  revocable and backed by the company's identity provider rather than by a
  token pasted into a browser.
- CyberdyneAuth now exists as the company's OIDC provider, with client roles and
  a written integration contract.

Today's model has no roles: every operator can do everything
(`admin/auth.py` 74-85), including the destructive calls (opt-out purge,
enabling a state-changing federated tool).

## What Changes

- **BFF login.** `GET /auth/login` starts authorization code + PKCE (S256)
  against CyberdyneAuth. `GET /auth/callback` exchanges the code server-side,
  creates a server-side session and sets a `__Host-cf_admin` cookie holding an
  opaque id. The browser never sees an access, refresh or id token.
- **Token verification** exactly as the CyberdyneAuth contract states: RS256
  via JWKS, `iss` from discovery, `exp`, `type == "access"`,
  `aud == "cyberfriend"`, users keyed on `sub`, email from userinfo.
- **Roles.** `<client_id>:admin` = admin (read/write). `<client_id>:operator` =
  operator (read-only). An absent roles claim means unknown and is denied. Each
  API route declares its minimum role in one table, and a request below that
  role gets 403.
- **Sessions.** Access tokens last 15 minutes and refresh tokens 30 days,
  rotating with reuse detection. Refresh is done server-side and serialised per
  session. Logout revokes the BFF session, then redirects to `end_session` with
  `id_token_hint`.
- **CSRF and framing.** SameSite=Strict, a required custom header on every
  non-GET request, an Origin check, and security headers (CSP with
  `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`).
- **Static tokens** keep working for scripts. A `cfa_` token maps to admin. A
  request carrying both a bearer and a session cookie is refused.
- **Audit attribution.** A console change made through OIDC is recorded as
  `oidc:<sub>`, with the email in a new display column.
- The console (Svelte) signs in with a "Sign in with CyberdyneAuth" button,
  shows the signed-in person and role, and hides controls the role cannot use.
- The admin process gains outbound HTTP (discovery, JWKS, token, userinfo,
  revocation) through an injected transport, like the bot's Edges.

Non-goals:

- The user (non-admin) web area. That is `add-account-provisioning-and-user-area`,
  and it uses a separate cookie that never satisfies an admin route.
- Retiring static tokens. That is a later, dated decision.

## Capabilities

### New Capabilities

- `console-authentication`: how a person signs in to the console, how their
  role is decided, and how a cookie session is protected.

### Modified Capabilities

- `admin-console`: "Operator identity comes from the credential" now covers an
  OIDC session as well as a bearer token, and adds roles.

## Impact

- New migration: `admin_session` table, and a nullable
  `config_audit.operator_display` column (the append-only trigger is kept).
- `src/chatmemory/admin/auth.py`: `Principal` replaces the role-less
  `Operator`; the middleware accepts a cookie or a bearer, never both.
- `src/chatmemory/admin/server.py`: `/auth/*` routes, a route-to-role table, security
  headers.
- `src/chatmemory/entrypoints/admin.py`: new configuration (issuer, client id,
  client secret, session-encryption key, public base URL) and a transport
  parameter. The "database credentials only" posture changes; the design
  records why.
- New dependency: authlib (or PyJWT + cryptography) server-side. No browser SDK.
- `docker-compose.yml` `admin` service: new environment variables.
- `docs/admin-console.md`, `docs/operations.md`.
