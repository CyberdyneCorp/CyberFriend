## 1. Reading

- [x] 1.1 `Node.native_balance`; batch-aware `rate_limited`
- [x] 1.2 Back-off on `ChainReader` (the balances path), with a regression test
- [x] 1.3 `AaveReader.reserve_tokens` and a per-process `ReserveCache`
- [x] 1.4 `PortfolioReader`: wallet over named tokens and Aave reserves, positions, Aave; one node per chain per lookup
- [x] 1.5 One retry per section under one deadline; a failure as a value

## 2. Totals and the answer

- [x] 2.1 `total`: per-asset Aave net, fees included, missing sections named
- [x] 2.2 Oracle prices, CoinGecko for ether only when the oracle fails, pegged stables
- [x] 2.3 Render per wallet, per chain, subtotal, grand total, exclusions; EN and PT
- [x] 2.4 Tests: no double counting, non-collateral supply, partial totals, two wallets

## 3. Tool and clearance

- [x] 3.1 `portfolio_summary` on `defi_positions`, read-only, allowlisted
- [x] 3.2 `clear_addresses`: every piece an address, at most three, budgeted per set
- [x] 3.3 Server timeout covers the portfolio deadline

## 4. Routing

- [x] 4.1 `portfolio_question`, EN and PT, with the "e no total?" follow-up
- [x] 4.2 `routing_crypto`: labels and one precedence; service answers from one table
- [x] 4.3 Bare "what's my balance?" uses the saved wallet
- [x] 4.4 Tests: routing table both ways; the corpus is never reached

## 5. End to end and docs

- [x] 5.1 FakeChain answers balances, reserves, user reserves and oracle prices
- [x] 5.2 E2E: saved wallet in PT, a failing section, no wallet, the follow-up
- [x] 5.3 README, operations guide, self-description
