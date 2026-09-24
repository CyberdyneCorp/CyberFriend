## 1. Storage

- [x] 1.1 Migration 0021: `asset`, `direction`, `price_level`, `edge_percent`; wallet columns nullable
- [x] 1.2 Kind, state and per-kind target checks widened; price level and edge distance bounded
- [x] 1.3 Unique index rebuilt with nullable columns coalesced; insert's `ON CONFLICT` names it
- [x] 1.4 A distance on a watched position updates that alert
- [x] 1.5 Downgrade deletes price rows and folds `near_edge` back first
- [x] 1.6 Test: price rows without a wallet, duplicates, the cap across kinds, the upgrade, the downgrade

## 2. Deciding

- [x] 2.1 `AlertKind.PRICE`, `PriceTarget`, `PriceObservation`, `PriceFeed`, `edge_distance`
- [x] 2.2 Price crossing with the 0.5% band; near-edge state with the one-point band
- [x] 2.3 English and Portuguese messages with the price and its time, or the distance and edge
- [x] 2.4 Creation checks: a price alert carries nothing of a wallet; the edge distance is 1-50%
- [x] 2.5 Test: every transition, the #210171 distance, both languages

## 3. Reading

- [x] 3.1 `CoinGeckoProvider.latest`: one constant request, fresh-only cache, own limiter
- [x] 3.2 `AlertPrices` as the proposal's `PriceFeed` and the sweep's price reader
- [x] 3.3 The runner reads price alerts apart from the chain, one request per sweep

## 4. Asking

- [x] 4.1 Recogniser: price requests (BTC, ETH, direction, level in either notation) and near-edge requests
- [x] 4.2 Not a price question, a question about alerts, or an archive question
- [x] 4.3 Proposals with the price now or the distance to the nearer edge
- [x] 4.4 `/alert list` and the capability reply name the new kinds
- [x] 4.5 End to end: a price request confirmed and fired once in Portuguese; a near-edge warning fired once

## 5. Documentation

- [x] 5.1 README and `docs/operations.md`
