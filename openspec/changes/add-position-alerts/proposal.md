## Why

People here keep Uniswap ranges and Aave loans open, and the moment that
matters -- a range the price has left, a health factor sliding towards 1.0 --
is the moment nobody is looking. A scheduled task cannot watch for it: it
replays a question through the model every one to twenty-four hours, keeps
nothing about what the previous run found, and so cannot tell a change from a
repeat. Everything a watch needs is already decoded by the positions readers
(`in_range`, `health_factor`); what is missing is storing the condition and its
last state, and a cheap sweep that reads only what changed.

## What Changes

This change is delivered in two pull requests, and this proposal covers both.

**PR-A1, the engine, shipped dark** (this change's first half):

- **A `position_alert` table** (migration 0020): owner, chain, kind
  (`lp_range` or `aave_health`), the pinned position (protocol, token id, v3
  pool address or v4 pool id, token symbols, decimals and fee) or the health
  limit, the address cleared at creation, the message language, and the edge
  trigger's state: `state`, `state_since`, `last_value`, `pending_state`,
  `pending_count`, failures, last check and last message. Ten active alerts per
  person, enforced inside the insert; one watch per target; cascade on person
  deletion; purged on opt-out and when the saved wallet it watches is forgotten.
- **A pure evaluator**: fires on a state change after two agreeing reads for a
  range, on the first read for a health factor; re-arms a health alert only at
  the limit plus 0.05; tells a closed position once and disables the alert; a
  failed read never changes state and never messages.
- **A chain watcher** that reads every due alert in one Multicall3
  `aggregate3` per chain, through `Edges.http_transport`, on its own rate
  limiter, with a hard cap on calls per chain per sweep.
- **A runner and a loop** in the bot process beside the scheduled sweep, on the
  edges' clock, sending templated English or Portuguese direct messages through
  the scheduled-task messenger; closed direct messages disable the person's
  alerts.
- **Settings** `ALERTS_ENABLED` (default false) and `ALERT_SWEEP_SECONDS`
  (default 300, at least 60), declared in `docker-compose.yml`.

**PR-A2, the surface** (the second half):

- **Creating an alert by asking**, in English or Portuguese: "tell me when my
  LP goes out of range", "me avisa se o health factor cair abaixo de 1,3". A
  deterministic recogniser (`app/alert_intent.py`, route label
  `ALERT_CREATE`) runs before retrieval and before the positions and wallet
  routes. The address is one the asker typed (here or in a recent question of
  theirs) or their saved wallet. The chain is read once and the reply lists
  exactly what would be watched, with the reading now, and Confirm / Cancel
  buttons only the asker can press; nothing is stored until Confirm.
- **`/alert list` and `/alert delete`**, global commands usable in the server
  and in a DM, keyed on the interaction's user, answered privately in the
  client's language, and described by the capability reply where alerts are on.
- **Messages name `/alert list` and `/alert delete <id>`.**
- **With alerts off** (the setting, or no Infura key) a request is answered
  that alerts are not available here, never searched for.

The switch stays off by default; the operator turns `ALERTS_ENABLED` on in the
deployment's configuration.

Non-goals:

- **Liquidation protection.** A sweep every five minutes can miss a fast move;
  the message says so.
- **Positions opened later.** An LP alert is pinned to the positions open when
  it was made. Rediscovering on every sweep would cost up to twenty-five
  requests per chain per wallet.
- **"Near the edge", price and fee thresholds, other exchanges.**
- **Any model call on the check path.**

## Capabilities

### New Capabilities

- `position-alerts`: what may be watched, how a watch decides to speak, what
  leaves the deployment on a person's behalf with nobody asking, and what
  removes a watch.

### Modified Capabilities

None. The positions readers, the egress guard, scheduled tasks and personal
facts behave as before; the wallet-forget purge is a trigger on the new table's
side.

## Impact

- Migration 0020 adds one table, its indexes and three triggers.
- New: `ports/alerts.py`, `app/alerts.py`, `adapters/chain/watch.py`,
  `adapters/store/alerts_{sql,postgres}.py`.
- `DiscordTaskMessenger` takes an optional heading; `positions_render.orient`
  is made public for the watcher.
- With `ALERTS_ENABLED=false`, the default, nothing is built and nothing is
  started, and an alert request gets a fixed "not available" reply.
- PR-A2 adds `app/alert_intent.py`, `app/alert_requests.py`,
  `adapters/chain/alert_targets.py`, `adapters/discord/alerts.py` and
  `adapters/discord/views.py` (the requester-only button check, now shared
  with the tool-approval prompt). `AskOutcome` carries an optional proposal;
  the positions reader's result carries each position's pool reference, which
  the positions answer does not show.
- PR-A2 registers `/alert list` and `/alert delete` on every deployment, as
  `/schedule` is: with alerts off they answer that the feature is off, and a
  typed "/alert list" gets the "use the slash menu" reply like any other
  command name. They are listed by the capability reply only where alerts are
  on.
- PR-A2 adds a last line to every alert message from the PR-A1 engine naming
  `/alert list` and `/alert delete <id>`; visible only where alerts are on.
- An alert request is recognised only in a question somebody is present for.
  A scheduled task whose question reads as an alert request is answered as any
  question, as before this change, whether alerts are on or off.

## Risk

This is the third feature that messages somebody unprompted, and the first
that sends a stored address to a third party on a timer with nobody asking.
The address is checked once, at creation, rather than on every read, which is
a deliberate exception to the per-call egress guard; it is bounded by the
switch, the declared hosts, and the rule that only the person's own saved or
typed address can be stored. Flapping is the other risk: an assistant that
messages every sweep gets muted, and muting it silences the obligation
notifications people need, which is what the confirmation and hysteresis rules
are for.
