## Why

The privacy dashboard and feature requests are reachable only from Discord.
People also want a place to see and manage them outside a chat. CyberdyneAuth
is the company's identity provider, but it has no Discord login, so a Discord
person has no way to get an account or prove which Discord person they are on
the web.

## What Changes

- **Consent in DM.** "Create my CyberdyneAuth account" (command, or offered
  from `/privacy`) shows the exact email and name that will be sent, taken from
  the person's facts or asked for, with [Confirm] [Cancel]. It records an
  `account_consent` row (version, time, hash of the email). Nothing else is
  ever sent: no phone, address, birth date or wallets.
- **`AccountProvisioner` port**: `ensure_account(ProvisioningRequest(email,
  name, external_ref))`, with `external_ref` = HMAC of the person id. The real
  adapter uses client_credentials through the Edges transport, against an
  endpoint CyberdyneAuth has not yet defined (**open dependency**). Until
  then, a fake adapter serves e2e and the feature stays off.
- **No account-existence oracle.** The reply is the same whether the account
  was created or already existed.
- **Linking by proof, not by typed email.** The DM carries a single-use link
  code (hashed, 15 minutes). The web `/link` flow signs in with CyberdyneAuth
  and binds `sub` to the person only if the userinfo email is verified and
  equals the consented email. The bot then DMs "Linked to a***@domain, not
  you? [Unlink]".
- **User area** at `/me` on the admin API origin, with its own cookie
  (`__Host-cf_user`). A user session can never satisfy an admin route.
  - `GET /me/privacy`: the same inventory as `/privacy`, DM-level detail.
  - `POST /me/erase`: "delete everything". Needs a fresh sign-in
    (`max_age`) and the typed confirmation, and also unlinks and ends the
    session.
  - `GET/POST /me/feature-requests`: list and submit, under the same rules as
    Discord.
  - A Svelte "Me" area in the same bundle, with its own route guard.
- Erasure deletes the link and consent rows. Deleting the CyberdyneAuth account
  itself is another open dependency.

Non-goals:

- Discord login at CyberdyneAuth.
- Admin screens in the user area, or roles for plain users.

## Capabilities

### New Capabilities

- `account-provisioning`: consent, provisioning and linking a CyberdyneAuth
  subject to a person.
- `user-area`: what a signed-in person can see and do on the web.

### Modified Capabilities

None. It reuses `privacy-dashboard` and `feature-requests` through their
services, and `console-authentication` for OIDC mechanics.

## Impact

- Migrations: `account_consent`, `account_link_code`, `person_account_link`,
  `user_session`, all purged by erasure and opt-out.
- `ports/accounts.py` (`AccountProvisioner`), `adapters/accounts/cyberdyneauth.py`
  (blocked), `tests/e2e/harness` fake provisioner.
- The bot process gains a client_credentials secret for provisioning. It is
  kept separate from the admin process's BFF client secret, if CyberdyneAuth
  allows two clients.
- Admin API: `/link`, `/me/*` routes, and a user-session middleware on `/me`.
- Console: a Me area.
