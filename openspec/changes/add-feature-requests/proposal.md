## Why

People already tell the bot what they wish it did, in the channel, in
Portuguese and English. Those messages are lost: at best they are answered as
questions, and at worst they are routed to a feature that happens to match (an
"I'd like a feature that notifies me when..." looks like an alert request). The
team has no list of what people want.

## What Changes

- `/suggest text:<...>` records a suggestion directly.
- `/suggestions` lists your own suggestions and their status.
- **Natural language, explicit forms only.** A message that opens with one of
  a fixed set of explicit suggestion forms (PT: "tenho uma sugestão",
  "sugestão:", "seria legal se você"; EN: "I have a feature request",
  "feature request:", "it would be nice if you could") gets a proposal with
  [Record suggestion] [No, answer it] buttons. Nothing is stored without the
  press. Bare words such as "sugestão" in "qual foi a sugestão do João?" never
  match.
- **Everything else wins.** The suggestion step runs only after every other
  router has declined the message: market, crypto (wallet, activity,
  portfolio, DeFi), facts, alerts, said-by, catch-up, decisions, obligations,
  typed commands, web search and time. So "sugestão: me diga o preço do BTC"
  is answered as a price question.
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
- Suggestions are purged on opt-out and by "delete everything", through the
  shared `purge_person_derived` function.

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

- New migration: `feature_request`, whose rows are added to
  `purge_person_derived` (add-privacy-dashboard), so opt-out and erasure purge
  them.
- `app/feature_requests.py` (service), `app/suggestion_intent.py`
  (recognition), a store adapter, and a port.
- `app/ask.py` dispatch: `suggestion_intent` as the last step before the
  default corpus answer, consulting the router's decision for the remainder.
- `self_description.py`: two commands. `tests/e2e/snapshots/commands.json`.
- `admin/handlers/feature_requests.py`; the console screen.
- `tests/unit/test_routing_eval.py`: labelled collision cases.
