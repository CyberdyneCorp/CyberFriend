## Why

"Quanto eu tenho no total?" and "what's my portfolio worth?" are the questions
people ask most about their own money, and today they reach the corpus: neither
names a wallet or a position, so no chain route claims them, and retrieval
answers from a colleague's message about their project. Even asked the right
way, the answer is three separate reports -- balances, pools, Aave -- that the
person has to add up themselves, with the double-counting and the missing
pieces that implies.

A live read of two real wallets showed what a total has to get right: an Aave
supply appears as an aToken in the wallet and must not be counted twice; the
account's collateral minus debt leaves out a supply that is not collateral;
cbBTC held in the wallet was invisible to the named token set; and Infura rate
limits made the balances path report a chain unreadable that a retry read.

## What Changes

- **A `portfolio_summary` tool** on the `defi_positions` server. For one to
  three wallets it reads, per chain: the wallet's balances (native ETH, the
  named tokens and every Aave reserve asset), open Uniswap v3/v4 positions with
  uncollected fees, and the Aave net per asset. It answers with a line per
  chain, a subtotal per wallet when there are several, and a grand total in
  USD, in the question's language.
- **One source of prices per chain**: the chain's Aave oracle; CoinGecko only
  for ether when the oracle does not answer; a dollar stablecoin the oracle
  does not list at $1. Anything unpriced is named and left out of the sum.
- **Honest partial totals.** A section that fails is tried once more; still
  failing, the answer says "at least $X" and names the chain and section.
- **Routed before retrieval** with the label `PORTFOLIO`, in both languages,
  including "e no total?" after a chain question. The asker's saved wallet is
  summed with any address they typed about their own money.
- **The chain routes behind one precedence** (`app/routing_crypto.py`):
  PORTFOLIO, then the positions labels, then WALLET_BALANCE, each answered from
  one table in the answer service.
- **"What's my balance?"** alone is a wallet question about the saved wallet.
- **Language detection** counts "quanto" and "tenho" as Portuguese on every
  route, so a short question built on them ("quanto eu tenho?") is answered in
  Portuguese, fixed replies included, instead of being left unknown.
- **Back-off on the balances path**, the same as positions reads, and one node
  per chain shared by everything a portfolio lookup reads.

Non-goals:

- **Token discovery.** Tokens outside the named set and the Aave reserves are
  not found (no Blockscout token list in v1: it adds egress, spam tokens and
  stale balances for pennies). The answer says what is excluded.
- **Other protocols.** Aerodrome, SushiSwap, Uniswap v2 LP tokens and other
  Aave markets are not read, and the answer says so.
- **Advice, history, P&L.** Figures only, as today.

## Capabilities

### New Capabilities

- `portfolio-summary`: what a total is made of, how it is priced, what it says
  when part of it could not be read, and whose wallets it may cover.

### Modified Capabilities

- `wallet-balances`: a question asking for a total is the portfolio's, a bare
  "what's my balance?" uses the saved wallet, and a rate-limited balance read
  is retried.

## Risk

No new host: the portfolio reads Infura as balances do and Blockscout only for
v4 position IDs, as positions do. The address rules are the positions tool's,
extended to a few addresses in one call: each must be rooted in the question
or be the asker's saved wallet, the call is refused (not trimmed) if any piece
is not an address, and at most three are accepted.
