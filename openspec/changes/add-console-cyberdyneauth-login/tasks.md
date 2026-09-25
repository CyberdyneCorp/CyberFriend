## 1. Principal and roles (bearer only)

- [x] 1.1 `Principal(subject, display, roles, via)` replacing role-less `Operator`; `cfa_` -> admin while no issuer is configured, operator once it is
- [x] 1.2 Route-to-role table beside `api_routes()` with an explicit row for every mounted `(method, path)` (`public`, `user`, `operator`, `admin`, `admin_oidc`); no per-method default; a route without a row is refused
- [x] 1.3 403 `{"error":"requires admin"}`; 401 stays byte-identical
- [x] 1.4 Test: enumerate every mounted route; fail on a route without a row, on a non-GET route below admin, and on `GET /api/usage/people/{id}/questions` not `admin_oidc` (the last row lands with the usage change)
- [x] 1.5 Test: an operator principal gets 403 on POST /api/optouts and POST /api/federation/allowlist
- [x] 1.6 Test: with the issuer set, a `cfa_` token is operator and gets 403 on every admin route

## 2. OIDC BFF (backend only, JSON `/api/session`)

- [x] 2.1 Settings: issuer, client id, secret, session key, public base URL; off when the issuer is unset
- [x] 2.2 Transport parameter on `entrypoints/admin.build()`; discovery and JWKS client with kid-miss refetch
- [x] 2.3 Access-token verifier (RS256, iss, exp, type, aud, roles); absent roles -> no role
- [x] 2.4 Id-token verifier (RS256, iss, `aud == client_id`, exp, nonce equal to the login record's)
- [x] 2.5 Migration: `admin_session`, `admin_login` (state hash, nonce, verifier, binding hash, purpose, link code hash, max_age, expires, used)
- [x] 2.6 `/auth/login` sets `__Host-cf_login` (HttpOnly, Secure, SameSite=Lax, 10 min); `/auth/callback` requires it and clears it; `/auth/logout`; `GET /api/session`
- [x] 2.7 Callback requires `userinfo.sub == id_token.sub == access_token.sub`
- [x] 2.8 Cookie `__Host-cf_admin`; a bearer header wins and the `Cookie` header is stripped from that request
- [x] 2.9 Refresh within 60s of expiry under a per-session lock; failure ends the session; roles re-checked on every refresh (absent -> revoke + 401, neither role -> revoke + 403)
- [x] 2.10 CSRF header + Origin check; security headers on every response
- [x] 2.11 Audit actor `oidc:<sub>` + `config_audit.operator_display`; trigger still refuses UPDATE/DELETE
- [x] 2.12 Tests: wrong aud, wrong iss, `type != access`, expired, unknown kid, absent roles, replayed state, callback without or with a wrong `__Host-cf_login`, id-token nonce missing or wrong, id-token aud wrong, userinfo sub mismatch
- [x] 2.13 Tests: refreshed token with absent roles -> session revoked, 401; with neither role -> session revoked, 403; admin lowered to operator -> operator
- [x] 2.14 Tests: bearer plus a valid cookie is served as the bearer and the session is untouched; invalid bearer plus valid cookie is 401
- [x] 2.15 Test: two concurrent requests near expiry trigger exactly one refresh
- [x] 2.16 e2e: admin process + FakeOIDC; operator sees read-only, admin mutates, logout ends the session
- [x] 2.17 The issuer alone is the switch: unset with the other four still set starts with sign-in off (break-glass); set without them refuses to start
- [x] 2.18 Tests: two refreshes in a row present the rotated refresh token; a sign-out racing a refresh serves nothing and revokes the new refresh token; an access token is refused as an id token with client id `cyberfriend`
- [x] 2.19 Used and expired `admin_login` rows are dropped when the next sign-in starts

## 3. Console sign-in (after the Svelte swap)

- [x] 3.1 `sessionApi` (me, login, logout); `services/http.ts` uses `credentials: "same-origin"`, the CSRF header on writes, and never sets `Authorization`
- [x] 3.2 SessionVM; route `minRole`; hide mutating controls for operator
- [x] 3.3 Move the token sign-in behind "Use an operator token" into `services/breakGlassHttp.ts` (bearer, `credentials: "omit"`)
- [x] 3.4 Storage guard: `Authorization`/`Bearer` allowed only in `services/breakGlassHttp.ts`; `same-origin` only in `services/http.ts`
- [x] 3.5 Tests: cookie mode sends no `Authorization`; token mode sends `credentials: "omit"`

## 4. Docs and ops

- [x] 4.1 `docs/admin-console.md`: login, roles, token downscoping, CyberdyneAuth client registration, break-glass by rollback
- [x] 4.2 `docker-compose.yml` admin env; `docs/operations.md` secret list
