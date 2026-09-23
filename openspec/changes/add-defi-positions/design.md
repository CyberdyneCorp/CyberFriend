## Finding positions

**Uniswap v3.** The position manager is ERC-721 Enumerable, so the chain lists
an owner's positions: `balanceOf`, then `tokenOfOwnerByIndex` for each, then
`positions(tokenId)`. Most NFTs a regular LP owns are closed (zero liquidity);
those are counted and not listed.

**Uniswap v4.** The v4 position manager is *not* enumerable, and Infura limits
`eth_getLogs` to 10,000 blocks, so walking `Transfer` events from deployment is
thousands of requests. Token IDs come from Blockscout's public API instead
(`/api/v2/tokens/{manager}/instances?holder_address_hash=`), keyless on all
three chains. Each ID is then checked with `ownerOf` on-chain; an ID the chain
does not confirm is dropped. `balanceOf` is read on-chain too, so an explorer
outage is reported as "N v4 positions could not be listed", not as none.

**Aave v3.** The pool addresses provider is the one hard-coded address per
chain; pool, data provider and oracle are resolved from it. Reserves come from
`getReservesList`, per-user amounts from the data provider's
`getUserReserveData`, account totals and health factor from
`getUserAccountData`.

## Bounding the cost

Every per-item read goes through Multicall3's `aggregate3`, in chunks, so a
wallet with 134 position NFTs is a handful of requests rather than hundreds.
Plain JSON-RPC batches of that size were rejected by Infura's rate limiter in
testing; one `eth_call` carrying many calls was not.

Uncollected v3 fees are the exception: `collect` must be simulated with the
owner as `from`, which a multicall cannot do, so it is one `eth_call` per open
position.

Each chain is read concurrently and bounded by its own timeout; a chain that
fails is reported as unreadable, never as empty.

## Valuing

USD values come from the Aave oracle on the same chain (8-decimal USD), which
covers the assets these pools are made of (WETH, USDC, WBTC, cbBTC, ...). Native
ETH in a v4 pool is priced as WETH. A token the oracle does not know is priced
from the pool itself when the other side is priced; otherwise it is shown
without a USD figure rather than guessed. No new outside price source.

## Presenting

A price range is quoted the way people read it: in the stablecoin when one side
is a stablecoin, otherwise in ETH when one side is ETH, otherwise token1 per
token0. The answer is the adapter's own text (verbatim), like balances.

## Routing

`defi_question` recognises liquidity terms (pool, liquidity, LP, uniswap,
range) and lending terms (aave, borrow, supply, debt, collateral, health
factor), in English and Portuguese. It requires an address in the question or
a first-person reference ("my pools"), so "what did we decide about the pool"
stays a corpus question; conversation verbs keep it there too.

The route offers exactly one tool: `liquidity_positions`, `lending_positions`,
or `defi_positions` when both kinds are asked about. The model's only job is to
copy the address.
