## 1. Consent and the port

- [ ] 1.1 Migrations: `account_consent`, `account_provisioning_request`, `account_link_code`, `person_account_link`; each added to `purge_person_derived`
- [ ] 1.2 `ports/accounts.py`: `ProvisioningRequest(email, name, locale)`, `AccountProvisioner.request_account`, `ProvisioningRateLimited`, `ProvisioningUnavailable`
- [ ] 1.3 `PROVISIONING_EMAIL_KEY`; `email_hmac` helper; no plain email or sha256 stored anywhere
- [ ] 1.4 Fake provisioner in `tests/e2e/harness` (always 202); `ACCOUNT_PROVISIONING_ENABLED` flag
- [ ] 1.5 `/account` command: guild -> DM; DM shows exact name and email, the invitation rule and the "outlives delete everything" statement; Confirm/Cancel; consent row
- [ ] 1.6 Per-person limits (1 per 24h, 3 per 30 days); 429 -> "try later", not counted
- [ ] 1.7 [Link my account] / `/account link`: fresh 15-minute single-use code, earlier codes invalidated, 5 per day
- [ ] 1.8 Tests: only email/name/locale sent; one reply text for every outcome; second request within 24h refused and nothing sent; fourth in 30 days refused; code hashed, single use, 15 min; consent text snapshot (EN/PT)

## 2. Linking and the user session

- [ ] 2.1 `/link` flow on the BFF: browser-bound login record with the code hash, `prompt=login`, `email_verified` + HMAC email match + `userinfo.sub` equals token `sub`
- [ ] 2.2 Post-link DM with Unlink (bot sweep reads new links)
- [ ] 2.3 `user_session` (with `fresh_auth_at`) + `__Host-cf_user`; `/me/*` rows in the route table; `/me` middleware; CSRF; add to `purge_person_derived`
- [ ] 2.4 Tests: unverified email refused; different email refused; sub mismatch refused; callback in another browser refused; used/expired/superseded code refused; each cookie 401 on the other's routes

## 3. User area

- [ ] 3.1 `GET /me/privacy`, `GET/POST /me/feature-requests`
- [ ] 3.2 Fresh sign-in with `max_age=300`; `auth_time` from the verified id token; `fresh_auth_at`
- [ ] 3.3 `POST /me/erase {mode, confirm}`: fresh auth within 5 min, typed word, CSRF; both modes; session ended, cookie cleared
- [ ] 3.4 Svelte `me/` routes, `userApi.ts`, VMs; architecture test forbids admin/user cross-imports
- [ ] 3.5 Tests: stale `fresh_auth_at` refused; id token without `auth_time` after `max_age` refused
- [ ] 3.6 e2e: FakeDiscord consent -> FakeOIDC link -> /me/privacy -> erase

## 4. CyberdyneAuth adapter

- [ ] 4.1 `adapters/accounts/cyberdyneauth.py`: `client_credentials` token (scope `users:provision`), `POST /api/v1/users/provision`, 202 -> ok, 429 -> rate limited; tested against a MockTransport of the approved contract
- [ ] 4.2 BLOCKED: CyberdyneAuth deploys the endpoint and issues the provisioning client -> enable `ACCOUNT_PROVISIONING_ENABLED`
- [ ] 4.3 OPEN DEPENDENCY: CyberdyneAuth expires unaccepted provisioned accounts after N days
- [ ] 4.4 OPEN DEPENDENCY: account deletion API -> call from erasure; until then the reply says how to request deletion
