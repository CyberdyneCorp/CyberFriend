## ADDED Requirements

### Requirement: BTC and ETH have an on-chain fallback source

The system SHALL price BTC and ETH from CoinGecko first only when a CoinGecko
API key is configured, sending it as a request header and never logging it,
and SHALL otherwise, and on any CoinGecko failure (an HTTP error status, a
body that is not the expected JSON, or no price for the asset asked), read the
Chainlink BTC/USD and ETH/USD price feeds on Ethereum mainnet through the
configured chain node. A Chainlink figure SHALL name Chainlink as its source,
SHALL be dated by the feed's own update time, and SHALL be marked stale when
that time is older than the feed's heartbeat plus a small margin. The feed
read SHALL be the same request whichever asset is asked. When no source
answers, the answer SHALL say no current figure is available and SHALL NOT
substitute one.

#### Scenario: CoinGecko refuses with a 403
- WHEN a CoinGecko key is configured, CoinGecko answers a CloudFront 403 HTML
  page, and a person asks "qual o preço do bitcoin?"
- THEN the answer SHALL give the BTC price from Chainlink with the feed's
  update time as its quote time

#### Scenario: No CoinGecko key
- WHEN no CoinGecko key is configured and a chain node is
- THEN CoinGecko SHALL NOT be contacted and the price SHALL come from Chainlink

#### Scenario: The key is only sent when set
- WHEN a CoinGecko key is configured
- THEN the CoinGecko request SHALL carry it in the `x-cg-demo-api-key` header
  and not in the URL
- AND WHEN none is configured, no such header SHALL be sent

#### Scenario: A stale feed
- WHEN the feed's last update is older than its one-hour heartbeat plus the
  margin
- THEN the figure's time SHALL say it is stale and may not be current

#### Scenario: Every source fails
- WHEN CoinGecko and the chain node both fail
- THEN the answer SHALL say no current figure is available
