## Why

People already tell the bot what they wish it did, in the channel, in
Portuguese and English. Those messages are lost: at best they are answered as
questions, and at worst they are routed to a feature that happens to match (an
"I'd like a feature that notifies me when..." looks like an alert request). The
team has no list of what people want.

## What Changes

- `/suggest text:<...>` records a suggestion directly.
- `/suggestions` lists your own suggestions and their status.
- **Natural language.** An explicit suggestion marker (PT: "sugiro que você",
  "sugestão", "seria legal se você", "gostaria de uma funcionalidade", "você
  deveria poder"; EN: "I suggest", "feature request", "I'd like a feature", "it
  would be nice if you could", "you should be able to") gets a proposal with
  [Record suggestion] [No, answer it] buttons. Nothing is stored without the
  press.
- The detector defers to fact, alert, catch-up and obligation intents, so "sugiro
  que você me diga o preço do BTC" is still answered.
- **What is stored.** The person's own words (up to 1000 characters),
  language, source (command, DM or channel ids) and status. Text containing an
  email, phone number or wallet address is refused, with the reason given.
  Resubmitting the same suggestion is idempotent, and each person can submit
  at most 5 a day.
- **Admin console.** `GET /api/feature-requests` (operator) and
  `PATCH /api/feature-requests/{id}` (admin) set the status (new, triaged,
  planned, done, declined, duplicate), an admin note and a duplicate-of link.
  Every change is audited. The Svelte console gets a Feature requests screen.
- **Status DMs.** When a status changes and the person opted in, a sweep in the
  bot process sends a DM, respecting notification preferences.
- Suggestions are purged on opt-out and by "delete everything".

Non-goals:

- Voting or public boards.
- Automatic grouping across people. Hash grouping is shown in the admin view,
  and embeddings may come later.

## Capabilities

### New Capabilities

- `feature-requests`: capture, storage, listing, triage and notification of
  suggestions.

### Modified Capabilities

None. `self-description` already lists commands from one table, and the new
commands are added there.

## Impact

- New migration: `feature_request`, with the opt-out purge trigger.
- `app/feature_requests.py` (service), `app/suggestion_intent.py`
  (recognition), a store adapter, and a port.
- `app/ask.py` dispatch: `suggestion_intent` after typed_command, before
  resolve_viewer and alert_intent. It is deferred in `said_by.py` and
  `routing.py`.
- `self_description.py`: two commands. `tests/e2e/snapshots/commands.json`.
- `admin/handlers/feature_requests.py`; the console screen.
- `tests/unit/test_routing_eval.py`: labelled collision cases.
