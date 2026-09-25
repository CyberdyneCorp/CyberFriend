## Why

The privacy dashboard and feature requests are reachable only from Discord.
People also want a place to see and manage them outside a chat. CyberdyneAuth
is the company's identity provider, but it has no Discord login, so a Discord
person has no way to get an account or prove which Discord person they are on
the web. CyberdyneAuth has now approved a provisioning contract for exactly
this.

## What Changes

- **Consent in DM.** "Create my CyberdyneAuth account" (`/account`, or offered
  from `/privacy`) shows the exact name and email that will be sent, taken from
  the person's facts or asked for, with [Confirm] [Cancel]. The consent text
  also says that the account and its email will outlive "delete everything"
  until CyberdyneAuth offers deletion, and how to ask for it. Nothing else is
  ever sent: no phone, address, birth date or wallets.
- **The approved CyberdyneAuth contract.**
  `POST /api/v1/users/provision` with a separate `client_credentials` client
  scoped `users:provision`, body `{email, name?, locale?}`. It always answers
  202 `{"status": "accepted"}`: no `sub`, no "exists" flag. CyberdyneAuth
  creates an unverified, passwordless account (`created_via =
  client:<client_id>`) and sends an invitation email; accepting it sets a
  password and verifies the email. An existing email gets a neutral notice at
  most once per 24 hours. CyberdyneAuth rate limits per client (429) and
  silently per email.
- **The email owner decides.** The invitation goes to the email's owner, who
  must accept it. A Discord person who types someone else's email never gains
  access to anything without that invitation.
- **Our own limits.** Per Discord person: at most 1 provisioning request per 24
  hours and 3 per 30 days. We store only `HMAC-SHA256(PROVISIONING_EMAIL_KEY,
  lowercased email)`, never the email or a plain hash.
- **`AccountProvisioner` port**: `request_account(ProvisioningRequest(email,
  name, locale))` returns nothing; a fake adapter serves e2e until the real
  adapter is enabled.
- **Linking by proof, not by typed email.** A single-use link code (15
  minutes), DMed by the bot on request, is redeemed on the web after
  CyberdyneAuth sign-in. The link is made only if `email_verified` is true, the
  email equals the consented email (by HMAC), and `userinfo.sub` equals the
  token `sub`. The bot then DMs "Linked to a***@domain, not you? [Unlink]".
- **User area** at `/me` on the admin API origin, with its own cookie
  (`__Host-cf_user`). A user session can never satisfy an admin route.
  - `GET /me/privacy`: the same inventory as `/privacy`, DM-level detail.
  - `POST /me/erase`: the same two delete-everything choices. Needs a fresh
    sign-in (`max_age=300`, `auth_time` read from the id token), the typed
    confirmation and the CSRF header; also unlinks and ends the session.
  - `GET/POST /me/feature-requests`: list and submit, under the same rules as
    Discord.
  - A Svelte "Me" area in the same bundle, with its own route guard.
- Erasure and opt-out delete the consent, provisioning-request, link-code,
  link and user-session rows through `purge_person_derived`. Deleting the
  CyberdyneAuth account itself is an open dependency.

Non-goals:

- Discord login at CyberdyneAuth.
- Admin screens in the user area, or roles for plain users.

## Capabilities

### New Capabilities

- `account-provisioning`: consent, provisioning, limits and linking a
  CyberdyneAuth subject to a person.
- `user-area`: what a signed-in person can see and do on the web.

### Modified Capabilities

None. It reuses `privacy-dashboard` and `feature-requests` through their
services, and `console-authentication` for OIDC mechanics.

## Impact

- Migrations: `account_consent`, `account_provisioning_request`,
  `account_link_code`, `person_account_link`, `user_session`, all added to
  `purge_person_derived`.
- `ports/accounts.py` (`AccountProvisioner`), `adapters/accounts/cyberdyneauth.py`
  (the approved contract), `tests/e2e/harness` fake provisioner.
- The bot process gains the provisioning client's id and secret (scope
  `users:provision` only) and `PROVISIONING_EMAIL_KEY`. They are separate from
  the admin process's BFF client.
- Admin API: `/link`, `/me/*` routes, and a user-session middleware on `/me`.
- Console: a Me area.
