"""Console sign-in through CyberdyneAuth, done by the admin API (a BFF).

The browser never runs OIDC and never holds a token: `/auth/login` starts an
authorization code + PKCE (S256) flow, `/auth/callback` finishes it
server-side, and the browser receives an opaque `__Host-cf_admin` cookie that
names a row in `admin_session`. Every request on that cookie re-verifies the
stored access token and takes the person's roles from it.

Modules, in the order a sign-in touches them:

*   `config` -- the settings; sign-in is off unless all of them are set.
*   `crypto` -- random values, hashes, and the AES-GCM cipher for tokens at rest.
*   `provider` -- discovery, JWKS, token, userinfo and revocation, all through
    one injected HTTP transport.
*   `verify` -- the access-token and id-token rules of the CyberdyneAuth contract.
*   `store` -- the login and session records, and in-memory stores for tests.
*   `service` -- the sign-in itself: begin, complete, authenticate, refresh, sign out.
*   `routes` -- `/auth/*` and `GET /api/session`.
"""
