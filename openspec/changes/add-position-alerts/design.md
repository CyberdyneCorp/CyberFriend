## Principle

An alert is a stored condition plus its last known state. A sweep reads the
chain in batched calls, a pure function decides, and a templated message goes
out only on a change. No model call, no `AskService`, and `scheduled_task` stays
as it is.

## Where it runs

The bot process, beside the scheduled sweep, for the reason scheduled tasks run
there: it holds the only connection a person can be messaged through. The loop
waits for the gateway, reads "now" from `Edges.clock`, and absorbs every error:
a claimed alert has already been advanced, so a failed sweep only means the
next one reads it.

## Claiming

`CLAIM_DUE` selects due, active alerts `FOR UPDATE SKIP LOCKED`, advances
`next_check_at` to `now + sweep` and returns the whole row in one statement --
the same shape as `scheduled_task`, so an outage is one check on return and not
a burst. It returns the row, not an id, because the evaluation needs the stored
state and reading it in the claiming statement makes the claim and the state it
acts on the same snapshot.

## The edge trigger

`evaluate(alert, reading, now) -> Evaluation(update, firing | None)` is pure,
and both kinds share one state machine:

- `UNKNOWN` (no baseline stored): the reading becomes the state, silently.
- The reading agrees with the state: pending is cleared, nothing is said.
- The reading disagrees: it becomes `pending_state` and `pending_count` counts
  agreeing reads. At `LP_CONFIRMATIONS` (2) for a range, or
  `HEALTH_CONFIRMATIONS` (1) for a health factor, the state changes and
  `state_since` moves.

What a change says:

| Kind | Change | Message |
|---|---|---|
| lp_range | in -> out | out of range |
| lp_range | out -> in | back in range (unless `notify_return` is off) |
| lp_range | any -> closed | closed; the alert is disabled `position closed` |
| aave_health | ok / no_debt -> below | dropped below the limit |
| aave_health | below -> ok / no_debt | recovered (unless `notify_return` is off) |
| aave_health | ok <-> no_debt | nothing |

A health factor that has fired stays `below` until it reaches the limit plus
`REARM_MARGIN` (0.05): 1.32 against a 1.30 limit does not re-arm, 1.35 does.

A closed position is liquidity 0, a burned NFT (`ownerOf` reverts), or an NFT
no longer held by the watched address. It is confirmed like any other change,
so one odd read cannot disable a watch.

A `ReadFailure` -- the chain unreachable, the pool not answering, the per-chain
cap reached -- leaves every state field as it was and increments
`consecutive_failures`, which a good read resets. It never messages.

The design's sixty-minute cooldown is left out: with confirmation and
hysteresis it would only suppress a genuine "back in range" that follows a
real "out of range" within the hour, and a suppressed transition is never told
because the state has already moved.

## Reading the chain

`ChainWatcher.observe(alerts)` groups by chain and makes one `aggregate3` per
chain through `Node.multicall`, which chunks at 50 calls. Per alert:

- v3: `ownerOf(id)` and `positions(id)` on the position manager, `slot0()` on
  the stored pool.
- v4: `ownerOf(id)`, `getPoolAndPositionInfo(id)` and
  `getPositionLiquidity(id)` on the position manager, `getSlot0(poolId)` on the
  StateView with the stored pool id.
- Aave: `getUserAccountData(address)` on the pool, resolved once per chain from
  the addresses provider and kept for the life of the process.

At most `MAX_CHUNKS_PER_CHAIN` (10) chunks per chain per sweep; alerts past that
are failures for this sweep and are read next time. Chains are read one after
another on the watcher's own `RateLimiter`, so a sweep never spends the spacing
interactive lookups rely on. A chain that fails is a failure for each of its
alerts and for nothing else. The Infura key is redacted from every log line.

Prices come from the ticks and the stored decimals, oriented the way the
positions answer orients them (`positions_render.orient`).

## Egress

The sweep sends stored addresses to `https://{mainnet,base-mainnet,
arbitrum-mainnet}.infura.io` with no ask run behind it, so the per-call
`clear_address` guard, which roots an address in the asker's words, has
nothing to check against. The check therefore moves to creation (PR-A2): an
alert may only be made on the person's saved `eth_wallet` fact or an address
they typed in the creating message, and the stored `address` column is the
result. `address_source` records which, so forgetting the saved wallet can
remove exactly the alerts that relied on it. The store, evaluator and watcher
in this change take an address that is already cleared.

## Messages

Deterministic templates in English and Portuguese, the language fixed at
creation and stored on the row. Prefixed with "🔔 **Alert**" / "🔔 **Alerta**"
in the text itself, so the messenger is handed an empty heading. Numbers in the
reader's notation (3,160.20 / 3.160,20). The last line names `/alert list` and
`/alert delete <id>`.

## Data model

`position_alert` (migration 0020): see the proposal for the column list. The
checks mirror the service: kind, chain, lowercase address, address source,
language, state, a threshold between 1.05 and 5.0, and a per-kind check that
each kind carries exactly its target columns. Indexes: a partial due index, an
owner index, and a partial unique index over (person, kind, chain, address,
token id, threshold) for active rows, which the insert names in `ON CONFLICT`.

Erasure, all in the database so no path can forget it:

- person deletion cascades;
- opting out deletes every alert, and a trigger refuses new ones;
- deleting the `eth_wallet` fact, or saving a different one, deletes that
  person's alerts with `address_source = 'saved'` on the old address.

## Cost

About three requests a sweep for a few alerts on two chains: 864 a day at the
default five minutes. A full positions lookup is two to twenty-five requests
per chain per wallet, and the sweep never runs one.

## Creating an alert (PR-A2)

### Recognising the request

`alert_intent(text, previous_questions) -> AlertIntent | None` in
`app/alert_intent.py`, lexical and model-free like the rest of routing. Its
route label is `ALERT_CREATE`, the name the unified router will give it; until
that router lands it is dispatched in `AskService.ask`, after facts, the
indexing pointer and typed commands, and before either answer path -- so before
retrieval and before the positions (`DEFI_*`) and wallet (`WALLET_BALANCE`)
routes, which is the precedence the router will keep:
`ALERT_CREATE > WALLET_ACTIVITY > PORTFOLIO > DEFI_* > WALLET_BALANCE`.

- An alert verb ("alert/notify/warn/ping me when|if", "set up an alert",
  "avise/me avisa quando|se", "quero um alerta") is a request whatever follows.
  "Tell me / let me know / me diz" is one only with "when"/"quando", or with a
  change named after "if"/"se" ("drops", "goes out", "cair", "sair"): "tell me
  if my health factor is ok" is a reading, and goes to the positions route.
- Range: a liquidity word (LP, pool, position, Uniswap, v3/v4) and leaving the
  range ("out of range", "sair da faixa", "fora do range").
- Health: "health factor", "HF", "fator de saúde". The limit is the number
  after "below / under / to / abaixo de / para", else the only number, with
  "1,3" read as 1.3; addresses, `#ids` and `v3/v4` are not numbers. A limit out
  of bounds is carried as written so the reply can state the bounds; none at
  all is answered by asking for one.
- A chain named with a preposition ("on base", "na arbitrum") narrows the
  read; `#4558452` names one position.
- Conversation verbs ("said", "discussed", "disseram") veto, as for the wallet
  routes: "what did people say about alerts" is a corpus question.

### The address, checked once

The address is the one in the message, or one the asker typed in one of their
last three questions (`routing.recent_chain_address`, the positions
follow-up's rule), stored as `typed`; else their saved `eth_wallet` from
`asker_values`, stored as `saved`; else the reply asks for one. Retrieved text
and other people's messages are never read. This is the clearance the sweep
relies on.

### The proposal

`AlertRequests.propose` checks the limit and the cap before reading anything,
then reads the chain once through `AlertTargets` (`ChainTargets`: the full
`UniswapReader` for ranges, `getUserAccountData` per chain for health, chains
one after another on their own rate limiter, a failing chain named in
`unreachable`). `LiquidityPosition` now carries `pool_ref` (the v3 pool from
`getPool`, or the v4 pool id), so the sweep never discovers anything. The
baseline uses `watch.lp_observation`, the sweep's own function.

The reply lists each target with its reading now; a target already past its
condition says so and that the next message comes when it changes back. It
names chains that could not be read and positions that could not be listed,
leaves out watches that already exist, cuts to the room under the cap and says
so, and says positions opened later are not covered. The wallet is named only
in a direct message. Nothing is stored.

### Confirming

The surface shows the proposal with Confirm and Cancel
(`adapters/discord/alerts.AlertConfirmView`), checked by the requester-only
view the tool-approval prompt also uses (`adapters/discord/views`). A reply to
the message in a DM or a channel; for `/ask`, whose public defer happened
before the request was recognised, the "thinking" response is deleted and the
prompt sent as an ephemeral followup. Confirm calls `AlertRequests.confirm`,
which creates each alert through `AlertService` (the store enforces the cap and
duplicates again) and replies with what was created, by id. Every outcome --
Confirm, Cancel, five minutes of nothing -- disables the buttons. The ledger
and desk behind tool approvals are not used: they exist to block a run until a
grant arrives, and nothing waits on this prompt.

The process's clock times the first check one sweep after creation, so the
harness's settable clock drives creation and sweep alike.

### Managing

`/alert list|delete` is a global group like `/schedule`, keyed on the
interaction's user, answered ephemerally in the client's language. There is no
`/alert create`: the request and its confirmation are the one way in, because
the confirmation is where the person sees what will be watched.
