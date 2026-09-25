## Context

CyberdyneAuth has no Discord provider and no confirmed provisioning endpoint
(the question is pending with that team). The admin API already has, from
`add-console-cyberdyneauth-login`, a BFF with OIDC code + PKCE, JWKS
verification and cookie sessions. The privacy inventory and erasure live in
`PrivacyService`, and feature requests in `FeatureRequestService`.

## Goals / Non-Goals

**Goals:**

- A person can get an account and reach their own data on the web without
  anyone being able to reach someone else's.
- Our side is complete and tested against a port, so the real adapter is a
  small change once the contract exists.

**Non-Goals:**

- Guessing CyberdyneAuth's API. The adapter is not written until the contract
  is.

## Decisions

### Consent in DM only

In a guild, the command replies ephemerally with "I'll DM you". In the DM, the
bot shows the exact values:

- email from the `email` fact, or asked for;
- name from `full_name`, else `preferred_name`, else the display name.

[Confirm] and [Cancel] are in a `RequesterOnlyView`. Confirm writes
`account_consent(person_id, consent_version, consented_at, email_sha256)` and
calls the provisioner. `ProvisioningRequest` is a typed record of exactly
`email`, `name` and `external_ref`, so no other fact can be passed by
accident.

### The port

```python
class AccountProvisioner(Protocol):
    async def ensure_account(self, request: ProvisioningRequest) -> None: ...
```

- It returns nothing on success, whether the account was created or existed.
  It raises `ProvisioningUnavailable` on failure.
- `external_ref = HMAC-SHA256(PROVISIONING_REF_KEY, person_id)` makes the call
  idempotent without exposing our ids.
- The adapter uses client_credentials and `edges.http_transport`.
- `ACCOUNT_PROVISIONING_ENABLED` defaults to false, and the command is hidden
  while it is off.

### No existence oracle

The DM is identical in both cases: "If you don't have an account yet you'll
get an email to set it up. Then sign in with the link below." The adapter
does not log created-versus-existing at a level visible outside ops. The same
applies to timing: the reply is sent after the call returns in both cases.

### Linking a subject to a person

The email typed in Discord is unverified, so a link is never made on it.

1. The DM carries a link `https://<admin host>/link?code=<c>`. `c` is 32
   random bytes. `account_link_code(code_sha256, person_id, email_sha256,
   expires_at = +15 min, used_at)`.
2. `/link` stores the code hash in the login record and starts OIDC with
   `prompt=login`.
3. On callback, the code must be unused and unexpired. Userinfo must have
   `email_verified = true` and an email whose sha256 (lowercased) equals the
   consent's. Then the callback inserts
   `person_account_link(person_id UNIQUE, issuer, sub UNIQUE, linked_at)` and
   marks the code used.
4. The bot DMs "Linked to a***@domain, not you? [Unlink]". Unlink deletes the
   link and revokes user sessions.

A forwarded or leaked code is useless without signing in as the consented,
verified email.

### User session

- Cookie `__Host-cf_user`, separate from `__Host-cf_admin`, in a
  `user_session` table with the same shape as `admin_session`.
- The `/me` middleware accepts only the user cookie. The admin middleware
  accepts only the admin cookie or a bearer. A test asserts that each cookie
  gets 401 on the other's routes.
- Any valid `cyberfriend` access token may open a user session. Roles are not
  consulted, because plain users may have `roles: []` or no claim at all (to
  confirm with CyberdyneAuth).
- Every `/me` endpoint resolves `person_id` from the session's `sub` through
  `person_account_link`. No endpoint accepts a person or platform id from the
  client.
- An unlinked `sub` and an unknown one get the same response: "No CyberFriend
  profile is linked to this account. Link one from Discord with /account."
- CSRF protection is the same as the admin console (custom header, Origin
  check, SameSite=Strict).

### Web erasure

`POST /me/erase` requires:

- an access token whose `auth_time` is within 5 minutes (the console re-runs
  OIDC with `max_age=300` first);
- the typed confirmation word in the body;
- the CSRF header.

It calls `PrivacyService.erase`, then deletes the link, consent and codes,
revokes the user session and refresh token, and clears the cookie. Deleting
the CyberdyneAuth account is not possible until that team provides an API.
Until then, the reply tells the person how to request it.

### Console

The Svelte bundle gains a `me/` route group with a `UserSessionVM`.
`PrivacyVM` and `MyFeatureRequestsVM` use `userApi.ts`, which has its own
service and never touches `adminApi.ts`. The architecture test forbids
imports between the admin and user view-models.

## Risks / Trade-offs

- [The external contract differs from our assumption] -> Only the adapter
  changes. The port carries the minimum (email, name, ref).
- [Account existence leaks] -> Identical reply and timing. No created flag
  crosses the port.
- [Wrong person linked] -> Requires a verified email equal to the consented
  one, a single-use 15-minute code, and a post-link DM with Unlink.
- [A user session reaches admin routes] -> Separate cookies and middlewares,
  with a cross-test.
- [Plain users' roles claim is unknown] -> The user area does not depend on
  roles.
