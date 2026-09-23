## Why

The assistant can say what a wallet holds, and cannot say what it has put to
work. For anyone providing liquidity or borrowing, the balance is the least
interesting number: the questions are whether a range is still earning, how
much is uncollected, and how close a loan is to liquidation. None of that is in
any channel, so asked today it is answered from whatever somebody once wrote.

## What Changes

- **Liquidity positions.** For an address, list its open Uniswap v3 and v4
  positions on Ethereum, Base and Arbitrum: pair and fee tier, what the position
  holds and its USD value, whether it is in range, the range's minimum and
  maximum price with the current price, and uncollected fees.
- **Aave positions.** For an address, list its Aave v3 supplied and borrowed
  assets per chain, with amounts, USD values and rates, plus the account's
  collateral, debt and health factor.
- **Arbitrum for wallet balances too.** One more chain in the existing lookup.
- **Routed before retrieval.** A question about somebody's pools or loans is
  sent to these tools and never to the corpus.

Non-goals:

- **Any transaction.** No collect, no withdraw, no repay. `collect` is
  *simulated* with `eth_call` to read uncollected fees; nothing is signed.
- **Other protocols and markets.** Uniswap v3/v4 and Aave v3's main market per
  chain only; SushiSwap, Aerodrome, Aave's Prime/EtherFi markets are not read,
  and the answer says so.
- **Advice.** Figures only, as with balances and prices.
- **History.** No P&L, impermanent loss or fee history: only current state.

## Capabilities

### New Capabilities

- `defi-positions`: which positions an address has, what is reported about
  each, and whose words the address may be.

### Modified Capabilities

- `wallet-balances`: Arbitrum is read as well.

## Risk

The address leaves in requests to Infura (as balances already do) and, for v4
position discovery only, to Blockscout's public explorer API. The rooting rule
bounds both: only an address the asker typed or saved is ever sent. Blockscout
is used to *find* token IDs and never trusted for what they hold; ownership and
every figure are read from the chain.
