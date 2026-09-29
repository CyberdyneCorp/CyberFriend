## ADDED Requirements

### Requirement: Price alerts use the crypto price fallback

Price alerts SHALL read BTC and ETH from the same sources in the same order as
the market tools' crypto price, so they keep working while CoinGecko refuses,
and SHALL treat a stale feed reading as no reading. A check without a fresh
price SHALL change nothing and send nothing.

#### Scenario: CoinGecko refuses
- WHEN CoinGecko answers 403 and an alert for BTC above 100,000 is checked
  while the Chainlink BTC/USD feed reads 100,412.35
- THEN the alert SHALL fire with the price, "Chainlink" as its source and the
  feed's update time

#### Scenario: Only a stale reading
- WHEN the only BTC price available is a feed reading older than its
  heartbeat plus the margin
- THEN the BTC alerts SHALL count a failed check and no message SHALL be sent
