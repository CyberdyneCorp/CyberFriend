## Context

`add-cyberwealth-federation` gave the `cyberwealth` server a service
principal and held `my_*` tools and personal results to DMs. CyberWealth's
partner guide: a connected-app key is created by the user (preset
`cyberfriend`: `finance:read` + `intel:read`), shown once, handed to the
partner privately, expires (90 days by default), can be revoked; every failure
is `401 auth.invalid_credentials`. `tools/list` returns only what the
credential asking may call. Key shape (CyberWealth's gitleaks rule):
`cwk_(live|test|dev)_<10 Crockford>_<43 url-safe>`.

## Decisions

### Recognising a key: strict to take, loose to withhold

`find_key` matches CyberWealth's exact shape and requires exactly one key in
the text. `mentions_key` is `"cwk_" in text.casefold()`: anything carrying the
marker is withheld from the archive and from the answer path. A truncated or
mistyped key is still a secret; a false positive costs one message not
archived and one warning.

### Only from a DM, answered before the answer path

`CyberFriendClient.on_message` checks `mentions_key` first, before the mention
check. In a DM the key is stored and the reply is fixed text naming the last
four characters; in a channel nothing is stored, nothing is answered, and the
author is DMed a warning (a channel warning would point everyone at the key).
Because this runs before `AskService.ask`, no model, tracer or memory sees the
text. `/connect` is registered with DM context only, so Discord does not list
it in the server; `/ask`, `/suggest` and `/schedule create` route text carrying
a key to the same handler (stored from a DM, refused in the server), always
ephemeral. The ingest side withholds `cwk_` in `withholds_personal_fact`, the
one conversion live messages, edits and backfill share; an edit that adds a
key retracts the stored message.

### Storage

`person_secret(person_id, kind, ciphertext, last4, created_at)`, primary key
`(person_id, kind)`, `kind IN ('cyberwealth')`. Connecting again replaces the
row. The adapter seals with AES-GCM (the `TokenCipher` the admin console uses
for tokens) under `PERSONAL_SECRETS_KEY`; associated data
`person_secret:<person id>:<kind>`, so a ciphertext copied to another row does
not open. A row that does not decrypt (rotated key, tampering) reads as no key
and logs an error; nothing is sent. `PERSONAL_SECRETS_KEY` is read by the bot
alone and declared on its service in compose; Coolify injects every variable
into every service, so nothing here assumes other processes lack it: only the
bot builds the cipher.

Opt-out and erasure: `purge_person_derived` (restated in full by 0037,
downgrade restores 0032's body) deletes the row; a BEFORE INSERT trigger drops
a key saved by an opted-out person, as 0030 does for suggestions.

### Where the key travels

`GuardedInvoker` looks the key up after authorization (which already refused
a personal tool outside a DM) and only for a permit with `personal` on a
server `PersonalKeys.covers` (`cyberwealth`). The lookup takes the requester
from the request -- the call site's identity, never the model's arguments. No
key: `Failure.NO_PERSONAL_KEY`, audited as refused, nothing sent.

`Federation.call(permit, arguments, bearer=key)` does not use the shared
session: `keyed_session_factory` opens a streamable-HTTP client with
`PersonalBearerAuth` (the key on each request of that client), makes the one
call and closes. The shared connection carries the service token and is used
by everyone, so a per-person bearer cannot ride it; a per-call connection costs
an `initialize` round trip, acceptable for a person's own question. The SDK
turns a `401` into a generic error, so the auth object records it and the
factory raises `PersonalKeyRejected` -> `Failure.KEY_REJECTED`. A keyed failure
never marks the server lost: one person's expired key must not remove the
shared tools. Errors are logged by type only.

Keys are turned on (`with_personal_keys`) only when `PERSONAL_SECRETS_KEY` is
usable and the `cyberwealth` server is configured over HTTPS (localhost
excepted).

### Registering tools the service principal cannot list

`register` treats a listed tool the server does not provide as a startup
error. For the `cyberwealth` server with keys on, a `my_` entry missing from
the service listing is registered from its name: description `"<words>: the
asker's own cyberwealth data, with their own key"`, an empty argument schema,
effect undetermined (the operator's `:ro` makes it read-only). Any other
missing tool is still a startup error.

### Forget and privacy

`/forget` deletes the key when run "everywhere", or in a DM (where the key was
given); "this conversation" in a server channel does not. `/privacy` reads the
service, last four characters and date from `person_secret` (never the
ciphertext). `ErasureCounts.keys` is new; older stored counts without it read
as 0.

## Risks

- A person who asks about their finances without a key gets an answer without
  that data and no hint in the answer; they learn of `/connect` from the
  capabilities reply and the docs (non-goal above).
- Rotating `PERSONAL_SECRETS_KEY` silently disconnects everyone.
- `mentions_key` withholds any channel message containing `cwk_`, including one
  that only talks about the prefix.
