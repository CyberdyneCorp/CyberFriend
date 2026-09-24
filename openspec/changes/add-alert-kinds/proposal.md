## Why

Position alerts watch a range and a health factor. Two more things people here
ask to be told about fit the same engine and cost almost nothing to add:

- **A price level.** "Avisa quando o BTC passar de 100k" today gets an ordinary
  answer with the current price, and nobody is told when it happens. The market
  tools already read BTC and ETH from CoinGecko with a constant request.
- **Nearing a range edge.** Leaving the range is the moment a position stops
  earning; the moment worth acting on is just before it. The live Arbitrum v4
  position #210171 sits about 3.4% under its upper edge, and the range alert
  says nothing until it is out.

## What Changes

- **Price alerts** (a new kind, `price`): "avisa quando o BTC passar de 100k",
  "me avisa se o ETH cair abaixo de 2500", "alert me when ETH goes above
  $3,000". Assets are the market tools' closed crypto vocabulary (BTC, ETH).
  The level is read in either notation ("100k", "2.500", "2,500", "$3,000",
  "120 mil"); a level with no direction is the side the price is not on yet.
  No wallet: `chain`, `address` and `address_source` are NULL for this kind
  only. Fires once when the price crosses the level in the direction asked, and
  re-arms, silently, 0.5% back on the other side.
- **Near-edge warnings** on range alerts: an optional `edge_percent` (1-50) on
  `lp_range`. A new state `near_edge` between in range and out of range; fires
  once on in range -> near the edge; out of range and back in range are told as
  before. The distance is measured on the oriented price to the nearer bound,
  and re-arms one percentage point further back. Asked on a position already
  watched, the distance is added to that alert instead of a second one.
- **Confirmations show the baseline**: the price now with its quote time, or
  the distance to the nearer edge ("3.4% from the upper edge").
- **Migration 0021**: new columns `asset`, `direction`, `price_level`,
  `edge_percent`; `chain`, `address`, `address_source` nullable; the kind,
  state and per-kind target checks widened; the unique index rebuilt with the
  nullable columns coalesced. Downgrade deletes price rows and folds `near_edge`
  back into `in_range` first.
- **One more egress request per sweep**: the constant CoinGecko request, sent
  once for every due price alert, through `Edges.http_transport`.
- `/alert list`, the confirmation and the capability reply name the new kinds;
  the cap of ten counts every kind.

Non-goals: other assets (the vocabulary is the market tools'), percentage-move
alerts ("if BTC drops 5%"), a near-edge alert on a position without a range
alert's other messages.

## Capabilities

### Modified Capabilities

- `position-alerts`: two more things to watch, one of them with no wallet.

## Impact

- `ports/alerts.py`, `app/alerts.py`, `app/alert_intent.py`,
  `app/alert_requests.py`, `adapters/discord/alerts.py`,
  `adapters/store/alerts_*.py`, `adapters/chain/watch.py`,
  `adapters/market/coingecko.py` (`latest`), new
  `adapters/market/alert_prices.py`, `composition.py`.
- Migration 0021 (data model).
- Egress: CoinGecko from the alert sweep, with no asker (see design).
