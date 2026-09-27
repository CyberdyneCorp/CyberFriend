## Why

CyberWealth answers its personal tools (`my_budget_status`, `my_unpaid_bills`,
...) only for a user-issued connected-app key (`cwk_live_...`) with the
person's `partner_agent:cyberfriend` consent. The service principal added by
`add-cyberwealth-federation` sees `intel_*` only, so today `my_*` cannot be
called at all. The partner guide (D10 item 2) asks for a DM-only, encrypted
per-person secret, sent only on that person's own calls and deleted by
`/forget`.

A key is a bearer credential to someone's household finances. It must not be
archived, traced, remembered, shown back or sent anywhere but CyberWealth on
the owner's own calls, and one pasted into a channel is already exposed.

## What Changes

- **Taking a key.** In a DM, a message containing one whole key ("minha chave
  do cyberwealth é cwk_live_...") or the new DM-only `/connect` command stores
  it for the author. Only a DM: elsewhere nothing is stored. The reply names the
  key by its last four characters and nothing more. A message carrying `cwk_`
  never reaches the answer path, so it is not sent to a model, traced or
  remembered.
- **Storage.** New table `person_secret` (migration 0037): AES-GCM ciphertext
  under a new server secret `PERSONAL_SECRETS_KEY` (32 bytes, base64, bot
  only), bound to the person id and kind as associated data, and the last four
  characters. Unset key: the feature is off and keys are refused.
- **Use.** A `my_*` call on the `cyberwealth` server, allowed only in the
  asker's DM (as today), carries the asker's own key as the bearer, over a
  connection opened for that call alone. `intel_*` calls and the shared
  connection keep the service identity. No key: nothing is sent and the call
  is refused. A `401` to the key is reported as a refused key, not as the
  server being down.
- **Registration.** A `my_*` tool allowlisted for the `cyberwealth` server is
  registered even though the service principal's listing does not show it.
- **Deletion.** `/forget` (everywhere, or in the DM), "Delete everything" and
  opt-out delete the key: `purge_person_derived` gains `person_secret`, and a
  key from an opted-out person is dropped by a trigger.
- **Privacy.** `/privacy` lists connected-app keys (service, last four
  characters, date) in a DM and their count in a channel; the erasure reply
  counts them.
- **Channels.** A channel message containing `cwk_` is never archived (live,
  edit or backfill), never answered, and its author is warned by DM to revoke
  the key. `/ask`, `/suggest` and `/schedule create` never store or ask text
  carrying a key.

## Non-goals

- Validating the key against CyberWealth when it is connected (`my_context`).
  The first personal call reports a refused key.
- Telling the asker, inside an answer, that they need to connect a key. The
  failure notice exists for the operator record; surfacing it to the asker is
  a reasoning-layer change.
- Deleting the channel message that carried a key (the bot may lack Manage
  Messages there); the warning tells the author to.
- Personal keys for any server other than CyberWealth.

## Impact

- New: `app/personal_keys`, `adapters/store/personal_keys_{sql,postgres}`,
  `adapters/mcp_client/personal_auth`, `adapters/discord/personal_keys`,
  migration 0037.
- `mcp_client`: `FederationConfig.personal_key_servers`, keyed calls in
  `Federation.call(bearer=...)`, `Failure.NO_PERSONAL_KEY` / `KEY_REJECTED`,
  `GuardedInvoker(personal_keys=...)`, unlisted personal tools in `register`.
- Discord: `/connect`, key handling in `on_message`, `/forget`, `/ask`,
  `/suggest`, `/schedule create`; ingest withholds `cwk_` messages.
- `/privacy` inventory and erasure counts; `PERSONAL_SECRETS_KEY` setting and
  compose declaration for `bot`.
