## 1. Principal and roles (bearer only)

- [ ] 1.1 `Principal(subject, display, roles, via)` replacing role-less `Operator`; `cfa_` -> admin
- [ ] 1.2 Route-to-role table beside `api_routes()`; non-GET defaults to admin; unknown route refused
- [ ] 1.3 403 `{"error":"requires admin"}`; 401 stays byte-identical
- [ ] 1.4 Test: every mounted non-GET route requires admin (walks the router)
- [ ] 1.5 Test: an operator principal gets 403 on POST /api/optouts and POST /api/federation/allowlist

## 2. OIDC BFF

- [ ] 2.1 Settings: issuer, client id, secret, session key, public base URL; off when the issuer is unset
- [ ] 2.2 Transport parameter on `entrypoints/admin.build()`; discovery and JWKS client with kid-miss refetch
- [ ] 2.3 Access-token verifier (RS256, iss, exp, type, aud, roles); absent roles -> no role
- [ ] 2.4 Migration: `admin_session`, `admin_login` (state hash, nonce, verifier, expires)
- [ ] 2.5 `/auth/login`, `/auth/callback`, `/auth/logout`; `GET /api/session`
- [ ] 2.6 Cookie `__Host-cf_admin`; middleware accepts cookie or bearer, refuses both
- [ ] 2.7 Refresh within 60s of expiry under a per-session lock; failure ends the session
- [ ] 2.8 CSRF header + Origin check; security headers on every response
- [ ] 2.9 Audit actor `oidc:<sub>` + `config_audit.operator_display`; trigger still refuses UPDATE/DELETE
- [ ] 2.10 Tests: wrong aud, wrong iss, `type != access`, expired, unknown kid, absent roles, replayed state, both credentials
- [ ] 2.11 Test: two concurrent requests near expiry trigger exactly one refresh
- [ ] 2.12 e2e: admin process + FakeOIDC; operator sees read-only, admin mutates, logout ends the session

## 3. Console

- [ ] 3.1 `sessionApi` (me, login, logout); `services/http.ts` uses `credentials: "same-origin"` and the CSRF header on writes
- [ ] 3.2 SessionVM; route `minRole`; hide mutating controls for operator
- [ ] 3.3 Storage guard: no `Authorization`/`Bearer` string in console sources; `same-origin` only in `services/http.ts`
- [ ] 3.4 Keep the token SignIn behind "Use an operator token" for break-glass

## 4. Docs and ops

- [ ] 4.1 `docs/admin-console.md`: login, roles, CyberdyneAuth client registration, rollback
- [ ] 4.2 `docker-compose.yml` admin env; `docs/operations.md` secret list
