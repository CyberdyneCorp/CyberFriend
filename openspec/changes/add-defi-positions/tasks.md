## 1. Reading

- [x] 1.1 ABI helpers and Multicall3 client (eth_call only, chunked)
- [x] 1.2 Uniswap v3 positions: enumerate, pool state, uncollected fees
- [x] 1.3 Uniswap v4 positions: Blockscout discovery, on-chain ownership check, fees from StateView
- [x] 1.4 Aave v3: account data, per-reserve supplies and borrows, rates
- [x] 1.5 Pricing from the Aave oracle, with pool-derived fallback
- [x] 1.6 Tests: math against known values, decoding, failure as a value

## 2. Tools and clearance

- [x] 2.1 `defi_positions` provider, rooted, not a closed vocabulary
- [x] 2.2 Three tools, the address checked like wallet balances
- [x] 2.3 Arbitrum added to wallet balances

## 3. Routing

- [x] 3.1 `defi_question` predicate, both languages
- [x] 3.2 Route before retrieval, offering exactly one tool; saved wallet used
- [x] 3.3 Tests: routing both directions; the corpus is never reached

## 4. Wiring and docs

- [x] 4.1 Registration, composition, wiring test
- [x] 4.2 Self-description lists the capability
- [x] 4.3 README
