## Context

CyberdyneAuth has no Discord provider. It has approved a provisioning contract
for this bot (below). The admin API already has, from
`add-console-cyberdyneauth-login`, a BFF with OIDC code + PKCE, a
browser-bound login record, id-token and JWKS verification and cookie
sessions. The privacy inventory and erasure live in `PrivacyService`, and
feature requests in `FeatureRequestService`. Every personal-data table is
purged through `purge_person_derived` (add-privacy-dashboard).

## Goals / Non-Goals

**Goals:**

- A person can get an account and reach their own data on the web without
  anyone being able to reach someone else's.
- Provisioning cannot be used to spam or open usable accounts in a stranger's
  name.
- The person is told, before consenting, what outlives "delete everything".

**Non-Goals:**

- Deleting the CyberdyneAuth account (no API yet).
- Any flow where the Discord identity alone grants web access.

## Decisions

### The approved CyberdyneAuth contract

- `POST /api/v1/users/provision`, authenticated with a separate
  `client_credentials` client whose only scope is `users:provision`. It is not
  the admin BFF client.
- Body `{email, name?, locale?}`. Nothing else.
- Response: always 202 `{"status": "accepted"}`. No `sub`, no "exists" flag.
  The response is the same whether the account was created, already existed,
  or was silently throttled.
- CyberdyneAuth creates an unverified, passwordless account with
  `created_via = client:<client_id>` and sends an invitation email. Accepting
  the invitation sets a password and verifies the email.
- An existing email gets a neutral notice at most once per 24 hours.
- Per-client rate limit: 429. Per-email throttle: silent (still 202).

**The invitation goes to the email's owner, who must accept it.** A Discord
person who enters someone else's address causes, at most, one invitation (or
one neutral notice per day) to that inbox. They never gain access: the account
is unusable until the invitation is accepted, and linking requires signing in
as that verified email. **Open dependency:** we ask CyberdyneAuth to expire
unaccepted provisioned accounts after N days, so an invitation that nobody
accepts leaves nothing behind.

### Consent in DM only

In a guild, the command replies ephemerally with "I'll DM you". In the DM, the
bot shows the exact values that will be sent:

- email from the `email` fact, or asked for;
- name from `full_name`, else `preferred_name`, else the display name;
- locale from the person's answer language.

The consent text, in the person's language, also states:

- CyberdyneAuth will email an invitation to that address, and the account
  works only after the invitation is accepted;
- the CyberdyneAuth account, with this name and email, is not deleted by
  `/privacy` "delete everything", because CyberdyneAuth has no deletion API
  yet, and how to ask its team to delete it.

[Confirm] and [Cancel] are in a `RequesterOnlyView`. Confirm writes
`account_consent(person_id, consent_version, consented_at, email_hmac)` and
calls the provisioner. `ProvisioningRequest` is a typed record of exactly
`email`, `name` and `locale`, so no other fact can be passed by accident.

### Email reference: HMAC, not a plain hash

`email_hmac = HMAC-SHA256(PROVISIONING_EMAIL_KEY, lowercase(email))`. A plain
sha256 of an email can be reversed by trying likely addresses; an HMAC cannot
without the server key. The key is a bot-process secret, never logged. The
same value is used in `account_consent`, `account_provisioning_request` and
`account_link_code`, and the admin process holds the key to compare at link
time.

### Our limits, per Discord person

`account_provisioning_request(person_id, email_hmac, requested_at)`:

- at most 1 request per 24 hours and 3 per 30 days per person, checked before
  the call; over the limit the reply says when they can try again and nothing
  is sent;
- a 429 from CyberdyneAuth is reported as "try again later" and is not counted
  as a request;
- these rows are purged by `purge_person_derived` and by a 30-day cleanup.

### The port

```python
class AccountProvisioner(Protocol):
    async def request_account(self, request: ProvisioningRequest) -> None: ...
```

- Returns nothing on 202. Raises `ProvisioningRateLimited` on 429 and
  `ProvisioningUnavailable` on anything else.
- The adapter obtains a `client_credentials` token (scope `users:provision`)
  and uses `edges.http_transport`.
- `ACCOUNT_PROVISIONING_ENABLED` defaults to false, and the command is hidden
  while it is off.

### Replies reveal nothing about the account

Because the contract always answers 202 with no existence flag, our reply is
necessarily identical in every case: "If this address doesn't have a
CyberdyneAuth account yet, it will receive an invitation. Once you've accepted
it, press [Link my account] and I'll DM you a sign-in link."

### Linking a subject to a person

The email typed in Discord is unverified, so a link is never made on it.

1. [Link my account] (in the consent DM, or `/account link`) DMs a fresh link
   `https://<admin host>/link?code=<c>`. `c` is 32 random bytes.
   `account_link_code(code_sha256, person_id, email_hmac, expires_at = +15 min,
   used_at)`. Issuing a new code invalidates earlier unused ones; at most 5
   codes per person per day.
2. `/link` stores the code hash in the login record (browser-bound by
   `__Host-cf_login`, as in the console login) and starts OIDC with
   `prompt=login`.
3. On callback, after the standard checks (state, browser binding, id-token
   nonce, `userinfo.sub == id_token.sub == access_token.sub`), the code must be
   unused and unexpired. Userinfo must have `email_verified = true` and an
   email whose HMAC equals the code's `email_hmac`. Then the callback inserts
   `person_account_link(person_id UNIQUE, issuer, sub UNIQUE, linked_at)` and
   marks the code used.
4. The bot DMs "Linked to a***@domain, not you? [Unlink]". Unlink deletes the
   link and revokes user sessions.

A forwarded or leaked code is useless without signing in as the consented,
verified email.

### User session

- Cookie `__Host-cf_user`, separate from `__Host-cf_admin`, in a
  `user_session` table with the same shape as `admin_session`, plus
  `fresh_auth_at`.
- `/me/*` is `user` in the route-to-role table. The `/me` middleware accepts
  only the user cookie. The admin middleware accepts only the admin cookie or
  a bearer. A test asserts that each cookie gets 401 on the other's routes.
- Any valid `cyberfriend` access token may open a user session. Roles are not
  consulted, because plain users may have `roles: []` or no claim at all.
- Every `/me` endpoint resolves `person_id` from the session's `sub` through
  `person_account_link`. No endpoint accepts a person or platform id from the
  client.
- An unlinked `sub` and an unknown one get the same response: "No CyberFriend
  profile is linked to this account. Link one from Discord with /account."
- CSRF protection is the same as the admin console (custom header, Origin
  check, SameSite=Strict).

### Web erasure

`POST /me/erase {mode, confirm}` requires:

- a fresh sign-in: the console re-runs OIDC with `max_age=300`. OIDC Core
  requires `auth_time` in the id token when `max_age` is requested. The
  callback verifies the id token (including nonce) and that `auth_time` is
  within 5 minutes, and records `user_session.fresh_auth_at`. Erase is refused
  unless `fresh_auth_at` is within 5 minutes. This does not depend on access
  tokens carrying `auth_time`;
- the typed confirmation word;
- the CSRF header.

`mode` is one of the two choices from `/privacy` ("delete everything", or
"delete everything and stop archiving me"). It calls
`PrivacyService.erase(person, mode)`, whose `purge_person_derived` step removes
the link, consent, provisioning-request, codes and user sessions. The response
clears the cookie and revokes the refresh token. The reply repeats that the
CyberdyneAuth account is not deleted and how to request it.

### Console

The Svelte bundle gains a `me/` route group with a `UserSessionVM`.
`PrivacyVM` and `MyFeatureRequestsVM` use `userApi.ts`, which has its own
service and never touches `adminApi.ts`. The architecture test forbids
imports between the admin and user view-models.

## Risks / Trade-offs

- [Provisioning used to spam a stranger's inbox] -> Per-person limits (1/24h,
  3/30d), CyberdyneAuth's per-email silent throttle and once-per-24h notice,
  and per-client 429.
- [An account opened in a third party's name] -> The account is unverified
  and passwordless until the email owner accepts the invitation; we ask
  CyberdyneAuth to expire unaccepted accounts.
- [Account existence leaks] -> The contract returns 202 in every case, so
  there is nothing to leak.
- [Wrong person linked] -> Verified email equal to the consented one (by
  HMAC), a single-use 15-minute code, a browser-bound login, a subject check,
  and a post-link DM with Unlink.
- [A user session reaches admin routes] -> Separate cookies, middlewares and
  route-table levels, with a cross-test.
- [The account outlives erasure] -> Stated before consent and after erasure.
