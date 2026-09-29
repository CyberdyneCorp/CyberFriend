## 1. Source

- [x] 1.1 Chainlink BTC/USD and ETH/USD mainnet feeds, addresses verified on chain (`description()`, `decimals()`)
- [x] 1.2 One constant Multicall3 read of `decimals()` and `latestRoundData()`; quote time `updatedAt`; stale past heartbeat + 5 min
- [x] 1.3 `COINGECKO_API_KEY` (SecretStr), sent as `x-cg-demo-api-key`, redacted from logs

## 2. Order and wiring

- [x] 2.1 Crypto price tool: CoinGecko with a key, else or on any failure Chainlink; CoinGecko alone without an Infura key
- [x] 2.2 Price alerts on the same provider; a stale reading is no reading
- [x] 2.3 Ether in wallet and portfolio answers on the same order; the portfolio names the fallback source
- [x] 2.4 Clock and transport from `Edges`; the crypto server's target lists the hosts it reaches

## 3. Tests and docs

- [x] 3.1 Unit: fallback order, key header only when set, CloudFront 403 regression, stale marking, decimals scaling, no key logged
- [x] 3.2 End to end: "qual o preço do bitcoin?" answered from Chainlink in Portuguese with CoinGecko refusing; a price alert offered and fired from Chainlink
- [x] 3.3 Live: both feeds' description, decimals and a fresh price (skips without `INFURA_KEY`)
- [x] 3.4 docker-compose, README, deploy-coolify, operations
