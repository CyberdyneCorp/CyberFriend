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

### Where recognition sits

`suggestion_intent` runs after `typed_command` and before `resolve_viewer`. It
reads nothing from the corpus, so it needs no viewer. It must come before
`alert_intent`, because "I'd like a feature that notifies me when X" matches
the alert strong trigger.

A match requires an explicit meta marker, and is dropped when `fact_intent`,
`alert_intent`, `catch_up_request` or `obligation_question` also matches the
remainder. A match never stores directly: it replies with a proposal and two
buttons, in a `RequesterOnlyView` with a timeout. [No, answer it] re-dispatches
the message past the suggestion step. A false positive therefore costs one
click. `said_by.py` 213-222 and `routing.py` 924-925 add the intent to their
deferral lists.

`/suggest` stores without a proposal, since the command is already explicit.

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
- A trigger on `person_opt_out` purges the person's rows, following 0014. An
  insert for an opted-out person is refused.
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

- [A natural-language match hijacks a real question] -> An explicit marker is
  required, the detector defers to other intents, a confirm button is needed,
  and there are labelled eval cases.
- [Contact details or third-party private text in suggestions] -> The detectors
  refuse contact details. The acknowledgement says the team sees the text.
- [Spam] -> Per-person rate limit and idempotent resubmission.
