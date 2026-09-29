## Why

On 2026-09-29 CoinGecko's keyless `https://api.coingecko.com/api/v3/simple/price`
started answering HTTP 403 with a CloudFront HTML error page ("The request
could not be satisfied ... Request blocked") to every caller -- the production
server, a developer laptop and GitHub runners alike -- while `/api/v3/ping`
still answered 200. CoinGecko was the only source for BTC and ETH, so the
`market_crypto:crypto_price` tool, BTC/ETH price alerts and ether's dollar
value in wallet answers (and in a portfolio whose Aave oracle did not answer)
all went dark.

## What Changes

- **A second source on chain.** The Chainlink BTC/USD
  (`0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c`) and ETH/USD
  (`0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419`) proxies on Ethereum mainnet,
  verified on chain (`description()` answers "BTC / USD" / "ETH / USD",
  `decimals()` 8). Read through the existing Infura node as one constant
  Multicall3 `eth_call` of `decimals()` and `latestRoundData()` for both feeds.
  Quote time is `updatedAt`; a reading older than the feeds' one-hour
  heartbeat plus five minutes is marked stale in its time line. Source label
  "Chainlink".
- **The order.** CoinGecko first only when the new optional
  `COINGECKO_API_KEY` (a secret; the free Demo plan, sent as the
  `x-cg-demo-api-key` header, never logged) is set; otherwise, and on any
  CoinGecko failure (HTTP error, 403/429, a body that is not the expected JSON,
  a missing price), Chainlink. Without an Infura key there is no Chainlink and
  CoinGecko is asked as before.
- **Same chain everywhere.** The price tool, price alerts and ether in wallet
  and portfolio answers share the order. An alert never fires on, and a
  confirmation never shows as "now", a stale feed reading. When every source
  fails the existing behaviour holds: no figure is made up or carried over.
- **Egress.** Every read goes through `Edges.http_transport`; the feed read is
  constant (both feeds, whichever asset is asked), carries nothing anybody
  wrote, and reaches only `mainnet.infura.io`, which the wallet tools already
  reach. The crypto server's declared target lists it. The closed vocabulary
  (BTC, ETH) is unchanged.

## Impact

- `adapters/chain/chainlink.py` (new), `adapters/chain/abi.py`,
  `adapters/market/coingecko.py`, `adapters/market/quotes.py`,
  `adapters/market/alert_prices.py`, `adapters/market/registration.py`,
  `adapters/chain/prices.py`, `adapters/chain/registration.py`,
  `adapters/chain/portfolio*.py`, `config.py`, `composition.py`,
  `entrypoints/bot.py`.
- New optional setting `COINGECKO_API_KEY` (bot), declared in
  `docker-compose.yml` and documented in the README, `docs/deploy-coolify.md`
  and `docs/operations.md`.
- No schema change.
