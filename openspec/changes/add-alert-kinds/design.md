## Context

`add-position-alerts` built an edge-triggered engine: a stored condition, its
last state, a pure evaluator, a batched reader, and a runner that messages on a
change. Both new kinds are new states and one new reading for that engine, not
a new engine.

## Decisions

### A price alert is a kind with no wallet

The alternatives were a separate table, or a price alert stored against a
dummy chain and address. A separate table would duplicate the cap, the claim,
the listing and the purges; a dummy address would put a fake wallet where the
wallet-forgotten trigger and the watcher look for a real one. So `chain`,
`address` and `address_source` become nullable, and the per-kind check requires
them for `lp_range` and `aave_health` and forbids them for `price`. Nothing that
reads a wallet can mistake a price row for one: the runner never hands a price
alert to the chain watcher, and the watcher treats a missing address as a
failed read.

The unique index coalesces the nullable columns, because NULLs are distinct in
a unique index and the same level twice would otherwise be two watches and two
messages.

### Reading prices: the market provider, one constant request per sweep

The sweep reads through `CoinGeckoProvider.latest`, a method on the market
tools' provider rather than a second client: the same endpoint, the same
constant request (`ids=bitcoin,ethereum`), the same fresh-only cache, its own
rate limiter, and `Edges.http_transport`. It is not a tool call, so it does not
pass through the egress guard; `adapters/chain/prices.py` already makes the
same request for the same reason: it has no variable part, so it carries
nothing about anybody, not even which asset is watched. Every due price alert,
BTC and ETH together, is answered by that one request, and none is sent when
no price alert is due.

The provider's cache never returns an expired figure, so a stale price can
neither be shown as a baseline nor decide a crossing; a sweep without a fresh
price records a failed check and says nothing.

Price alerts are available wherever alerts are (`ALERTS_ENABLED` with an Infura
key), whether or not `MARKET_TOOLS_ENABLED` is on: CoinGecko is already reached
by the portfolio's ether pricing on those deployments, so no new host appears.

### Crossing, not level

State is the side of the level (`above` / `below`). A message goes out when the
state changes to the side the alert fires on, on the first read (the price is
an aggregate, not a block's wick), and never while it stays there. Leaving the
fired side takes 0.5% of the level beyond it, so a price wobbling on the line
is one message; the re-arm itself is silent. A request with a level and no
direction ("when BTC hits 100k") fires on reaching it from where it is now.

### Near the edge is a range alert with a distance

Not a kind: a near-edge warning watches the same position with the same reads,
and two alerts on one position would send two "out of range" messages for one
change. So `edge_percent` is a column on `lp_range`, and asking for a distance
on a position already watched updates that row (`ON CONFLICT ... DO UPDATE`,
only when a distance is given and differs).

The distance is the move the displayed price must make to reach the nearer
bound, as a percentage of the price now: `high / price - 1` or
`1 - low / price`, on the prices `orient` produces for the positions answer. For
#210171 (tick -197404 in [-197920, -197070)) that is 1.0001^334 - 1 = 3.40% to
the upper edge.

`near_edge` goes through the same two-read confirmation as a range change.
It re-arms one percentage point further from the edge than the distance asked.
Out of range from `near_edge`, and back in range to either `in_range` or
`near_edge`, are told as they were.

## Risks / Trade-offs

- A person at the cap cannot add a distance to an alert they already have: the
  cap is a predicate on the insert, checked before the conflict. Deleting one
  frees the slot.
- The price is CoinGecko's aggregate, updated about once a minute; a five-minute
  sweep can miss a wick past the level that reverses within the interval. This
  is said in the confirmation's footer ("I check every N minutes").
