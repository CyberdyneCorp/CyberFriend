## 1. Reading

- [x] 1.1 `activity_rows`: advanced filters with the empty-string cursor, a repeat guard and a four-page cap
- [x] 1.2 `multicall_selectors`: inner calls of the wallet's position-manager multicalls
- [x] 1.3 `AaveReader.wrappers`: aToken and debt token per reserve, cached per process
- [x] 1.4 `ActivityReader`: explorers in parallel, node reads per chain, failures as values

## 2. Classification and the answer

- [x] 2.1 Legs per transaction; liquidity, lending, swap, sent/received, other
- [x] 2.2 Hidden rows (unsolicited, zero value, dust) and lookalike detection
- [x] 2.3 Gas from wallet-sent rows only; relayed transactions counted
- [x] 2.4 Verbatim render per chain, newest first, bounded, EN and PT, period stated

## 3. Tool, clearance and privacy

- [x] 3.1 `wallet_activity` on `defi_positions`, read-only, allowlisted
- [x] 3.2 `private` on `InvocationRequest`, `EgressRequest`, `AuthorizedQuery` and `Cleared`
- [x] 3.3 Window from the cleared question, never the arguments; at most 30 days

## 4. Routing

- [x] 4.1 `activity_question` EN/PT, carried address, "and last month?" follow-up
- [x] 4.2 `WALLET_ACTIVITY` first in `routing_crypto`; service row
- [x] 4.3 Tool description ranks under the production tool limit

## 5. Tests and docs

- [x] 5.1 Unit: classifier on reduced real rows, cursor regression, window, redaction, routing both ways
- [x] 5.2 E2E: DM with a saved wallet, the same in a channel, a failing explorer
- [x] 5.3 README, docs/operations.md, self-description
