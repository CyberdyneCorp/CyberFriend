## Context

The ask pipeline (`app/ask.py` 366-441) dispatches in a fixed order: limiter,
fact_intent, indexing_request, typed_command, viewer/ACL resolution,
alert_intent, catch-up and said_by, and then the answer services. Alert
proposals (ask.py 532-568) already use a propose-then-confirm button pattern.
The admin API has no content surface, and the audit log is append-only.

## Goals / Non-Goals

**Goals:**

- A suggestion is never lost and never stored by accident.
- Existing intents keep winning when they match.
- What is stored is the person's own words and nothing about the conversation
  around them.

**Non-Goals:**

- Public visibility of other people's suggestions.
- Replying to suggestions in the channel on the team's behalf.

## Decisions

### Explicit forms only

`suggestion_intent` matches only when the message, after the bot mention and
leading whitespace, starts with one of these forms (case- and
accent-insensitive):

- PT: "tenho uma sugestão", "sugestão:", "seria legal se você"
- EN: "I have a feature request", "feature request:", "it would be nice if you
  could"

The list is a constant with a unit test per form. There is no bare-noun or
imperative marker: "sugestão" alone, "sugiro que", "você deveria" and "you
should be able to" do not match, so "qual foi a sugestão do João?" and "you
should be able to tell me X" are never proposed as suggestions.

`/suggest` stores without a proposal, since the command is already explicit.

### Every other router wins

The suggestion step runs last, just before the default corpus answer, after
every existing step in `app/ask.py`: fact_intent, indexing_request,
typed_command, viewer/ACL resolution, alert_intent, catch-up, said_by. It then
asks the router (`routing.py`) for its decision on the remainder of the
message (the text after the form). Any decision other than the default corpus
answer wins: market, crypto (wallet balance, activity, portfolio, DeFi), web
search, time, decisions, obligations, capabilities. The suggestion step is not
a hand-kept deferral list, so a router added later wins automatically.

So:

- "sugestão: me diga o preço do BTC" -> market price answer.
- "it would be nice if you could show my portfolio" -> portfolio answer.
- "feature request: notify me when BTC hits 100k" -> alert proposal
  (alert_intent ran first).
- "tenho uma sugestão: avisar quando alguém me marcar" -> suggestion proposal.

### Always confirmed

A match never stores directly. It replies with a proposal and two buttons in a
`RequesterOnlyView` with a 120-second timeout. [Record suggestion] stores it.
[No, answer it] answers the message through the default corpus answer, as if
the suggestion step had not matched. On timeout the buttons are disabled and
the message is answered the same way, so the message is never left
unanswered.

### Data model (migration 0028)

```
feature_request(
  id bigserial PK,
  person_id FK person ON DELETE CASCADE,
  text text CHECK (length(text) BETWEEN 1 AND 1000),
  normalized_hash bytea, UNIQUE (person_id, normalized_hash),
  language text,
  source_kind text CHECK (source_kind IN ('command','dm','channel')),
  platform text, guild_id bigint NULL, channel_id bigint NULL,
  status text CHECK (status IN ('new','triaged','planned','done','declined','duplicate')) DEFAULT 'new',
  admin_note text NULL, duplicate_of bigint NULL REFERENCES feature_request,
  notify_on_change bool DEFAULT false, notified_status text NULL,
  created_at, updated_at, updated_by text NULL)
```

- There is no message text, surrounding context or channel name. The console
  resolves channel names itself.
- The migration adds `DELETE FROM feature_request WHERE person_id = $1` to
  `purge_person_derived` (add-privacy-dashboard), so admin opt-out and both
  delete-everything choices purge suggestions with no separate step. An insert
  for an opted-out person is refused.
- `normalized_hash` is sha256 of the text lowercased, with whitespace collapsed
  and punctuation stripped. A resubmission returns "already recorded (#12)".

### Text rules

- The text is refused when `states_own_contact` or `find_addresses` finds an
  email, phone or wallet. The reply says why and asks for the suggestion without
  it.
- Rate limit: 5 accepted suggestions per person per rolling 24 hours. The
  limit reply does not store anything.
- Acknowledgement (PT/EN): "Recorded as #12. The team will see your text and
  your Discord name. Tell you when its status changes? [Yes] [No]".

### Admin API and triage

- `GET /api/feature-requests?status&page`: operator. Returns id, text,
  language, status, note, duplicate-of, created/updated, person display name
  (joined at read time), source kind, and a count of other people with the same
  hash.
- `PATCH /api/feature-requests/{id}` with `{status?, admin_note?,
  duplicate_of?}`: admin. Requires the CSRF header like every console write.
  Writes a `config_audit` `applied` entry with setting
  `feature_request.<id>.status`, and before and after values. The note is not
  recorded in the audit, because it is free text.
- Suggestion text is the person's own words, given to the team on purpose with
  a disclosure, so it is not corpus content. The `admin-console` corpus rule is
  unaffected.

### Status DMs

The admin process cannot DM. A sweep in the bot process (every 10 minutes)
selects rows where `notify_on_change` is true and `status <> notified_status`,
and sends a localised DM. It skips people whose
`notification_preference.undeliverable_at` is set, and then sets
`notified_status`. Before the admin triage endpoint exists, the sweep has
nothing to send.

## Risks / Trade-offs

- [A natural-language match hijacks a real question] -> Only fixed explicit
  forms match, every other router wins, a confirm button is needed, and there
  are labelled eval cases.
- [Contact details or third-party private text in suggestions] -> The detectors
  refuse contact details. The acknowledgement says the team sees the text.
- [Spam] -> Per-person rate limit and idempotent resubmission.
