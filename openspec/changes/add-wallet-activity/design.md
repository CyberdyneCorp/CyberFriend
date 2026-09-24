## Context

The positions server already reads Blockscout (v4 position IDs) and Infura
(Aave, Uniswap). Activity adds one explorer endpoint and one Aave read, and a
field on the egress clearance.

## Decisions

### Advanced filters, not the address's transaction list

`/api/v2/advanced-filters?from_address_hashes_to_include=A&to_address_hashes_to_include=A&address_relation=or&age_from&age_to`
returns top-level transactions, internal value transfers and ERC-20/721/1155
transfers in one list, filtered by time on the server. The address list misses
every relayed action of an EIP-7702 account, and the internal-transactions
endpoint took 44 s or timed out on the same wallets.

### The cursor's null fields are sent as empty strings

`next_page_params` has null fields (`internal_transaction_index`). Left out,
the server returns page one again forever; "None" is HTTP 422; "" works. A
cursor seen twice ends the read anyway, and four pages (200 rows) is the cap --
a month of the busiest week measured -- with the cap stated in the answer.

### Classification is deterministic

Rows are grouped by transaction into legs (asset, signed amount, counterparty,
whether the counterparty is a contract). In order: liquidity (a position NFT
moved, or the v3/v4 position manager was called or paid), lending (an aToken or
debt token moved), swap (one asset out and another in), sent/received, other
(a transaction the wallet sent that moved nothing, named by its method).

- Liquidity sub-type from the multicall's inner selectors (one page of
  `/addresses/A/transactions?filter=from`, fetched only when such a multicall
  is present), or the NFT mint, or the flows; a relayed withdrawal with no
  input to read is "LP withdrawal (liquidity and/or fees)".
- Aave amounts are the underlying's: the aToken mint includes interest.
- An outgoing leg to a plain address inside a larger transaction -- a relayer's
  cut -- is its own transfer and is never called a fee.
- Gas is summed only from top-level rows the wallet sent; relayer-submitted
  transactions are counted separately.

### What is hidden

A token row is shown only when the token is recognised (named tokens, Aave
reserves, aTokens and debt tokens, position NFTs) or the wallet sent the
transaction, and its value is not zero; an inbound-only transaction from a
third party worth under $0.01 is dust. Explorer reputation is never used. A
hidden row whose counterparty shares the first and last four hex characters
with a counterparty shown is a lookalike and raises the warning.

### The window is the question's

The provider resolves the period from the cleared question (`Cleared.question`)
with EN and PT phrases and "last N days", default seven days, clamped to thirty
(thirty days plus the partial current day is within the limit).

### Privacy travels with the clearance

`InvocationRequest.private` is set by the loop from `question.audience.is_private`,
copied by the invoker into `EgressRequest`, minted into `AuthorizedQuery` by the
guard and read by `clear_address` into `Cleared.private`. Default false: a
provider reached any other way writes for a channel. The renderer prints full
addresses only when private; the header (quoted by the citation footer) follows
the same rule.

### Tool routing

With more read-only tools registered than a run may see, the tool router ranks
by shared words and the route narrows afterwards. The description therefore
carries the period words people ask with, and a regression test routes the real
registration at the production limit.

## Risks / Trade-offs

- Current prices, labelled. Historic ones are not cheap and only sometimes
  available.
- A lookalike older than the window is not detected; the rows are still hidden
  by the other rules.
- A counterparty that is itself an EIP-7702 account reports `is_contract`, so a
  payment to one inside a larger transaction is not split out as a transfer.
