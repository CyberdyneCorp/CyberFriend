## Why

"What did my wallet do this week?" is the question people ask after a balance,
and today it reaches the corpus: no chain route claims it, and retrieval
answers from a colleague's message. Asked with an address it goes to the
balance route, which answers a different question.

A live read of the two wallets it is for showed what an answer has to get
right. Both are EIP-7702 smart accounts: an Aave supply and an LP withdrawal on
one of them were submitted by a relayer, so neither appears in the explorer's
list of the address's transactions -- an answer built on that list would say
nothing happened. And both wallets receive address-poisoning spam that the
explorer rates "ok": homoglyph "USDC" tokens moved to lookalikes of a real
counterparty, zero-value `transferFrom`s, and dust from a lookalike.

## What Changes

- **A `wallet_activity` tool** on the `defi_positions` server, read-only and
  allowlisted like the positions tools. For one wallet it reads, per chain:
  Blockscout's advanced filters (top-level, internal and token transfers in or
  out of the address, filtered by time on the server), Aave's aTokens and debt
  tokens from the pool, and the Aave oracle's current prices.
- **Classified without a model**: swaps, Uniswap liquidity (open, add, remove,
  collect, or a relayed withdrawal), Aave supply, withdraw, borrow and repay in
  the underlying asset, transfers in and out, and the gas the wallet paid.
- **Spam hidden by rule and counted**: tokens nobody listed in transactions
  the wallet did not send, zero-value transfers, and third-party deposits under
  a cent. Hidden transfers whose counterparty copies a real counterparty's
  first and last four characters raise an explicit poisoning warning.
- **The window from the question**: EN and PT periods ("essa semana", "ontem",
  "nos últimos 30 dias", "this week"), seven days by default, at most thirty,
  and the period always stated in the answer.
- **Privacy by audience**: the egress clearance carries whether only the asker
  reads the answer (`private`), set from the question's audience. In a DM every
  counterparty is printed in full, never shortened; in a channel each is "an
  external address" and the wallet is named by its last four characters.
- **Routed before retrieval** with the label `WALLET_ACTIVITY`, ahead of the
  other chain labels: an address in the question, one carried from the asker's
  own earlier question, or the saved wallet -- and with several saved and none
  named, the asker is asked which.

Non-goals:

- **Historic prices.** USD is at current prices, and the answer says so.
- **Bridges and other protocols** by name. Their transfers appear as transfers
  or as a named method; no label is guessed.
- **Uniswap v4 action decoding.** A v4 transaction is named from its flows.

## Capabilities

### New Capabilities

- `wallet-activity`: what a wallet did in a window of days, how it is read and
  classified, what is hidden, and what a channel may see.

### Modified Capabilities

- None. The clearance's `private` field defaults to the channel's rule, so
  every existing provider behaves as before.

## Risk

No new host: Blockscout is already the positions server's target (for v4 IDs),
and Infura reads use multicall and the per-process reserve cache. Egress is the
positions tools': the address must be rooted in the question or the saved
wallet, and is refused rather than trimmed. The new privacy field fails
closed -- absent, an answer is written for a channel.
