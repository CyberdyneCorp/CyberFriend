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
reader's notation (3,160.20 / 3.160,20). No command is named until `/alert`
exists.

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
