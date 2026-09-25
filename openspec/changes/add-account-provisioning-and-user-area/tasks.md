## 1. Consent and the port (unblocked)

- [ ] 1.1 Migrations: `account_consent`, `account_link_code`, `person_account_link`; purged by erasure and opt-out
- [ ] 1.2 `ports/accounts.py`: `ProvisioningRequest(email, name, external_ref)`, `AccountProvisioner`
- [ ] 1.3 Fake provisioner in `tests/e2e/harness`; `ACCOUNT_PROVISIONING_ENABLED` flag
- [ ] 1.4 `/account` command: guild -> DM; DM shows exact values; Confirm/Cancel; consent row
- [ ] 1.5 Identical reply for created vs existing; link code DM
- [ ] 1.6 Tests: only email/name/ref sent; identical replies; code hashed, single use, 15 min

## 2. Linking and the user session

- [ ] 2.1 `/link` flow on the BFF: code check, `prompt=login`, `email_verified` + consented email match
- [ ] 2.2 Post-link DM with Unlink (bot sweep reads new links)
- [ ] 2.3 `user_session` + `__Host-cf_user`; `/me` middleware; CSRF
- [ ] 2.4 Tests: unverified email refused; different email refused; used/expired code refused; each cookie 401 on the other's routes

## 3. User area

- [ ] 3.1 `GET /me/privacy`, `GET/POST /me/feature-requests`
- [ ] 3.2 `POST /me/erase` with fresh auth (`auth_time`), typed word, CSRF; unlink and end session
- [ ] 3.3 Svelte `me/` routes, `userApi.ts`, VMs; architecture test forbids admin/user cross-imports
- [ ] 3.4 e2e: FakeDiscord consent -> FakeOIDC link -> /me/privacy -> erase

## 4. Blocked on CyberdyneAuth

- [ ] 4.1 BLOCKED: provisioning endpoint contract -> `adapters/accounts/cyberdyneauth.py` via Edges
- [ ] 4.2 BLOCKED: account deletion API -> call from erasure
- [ ] 4.3 BLOCKED: confirm roles claim for plain users (`[]` vs absent) and `auth_time` in access tokens
