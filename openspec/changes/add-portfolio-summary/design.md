## Where it lives

`portfolio_summary` is a fourth tool on the `defi_positions` server rather than
a new server, because it reads with the positions readers: one `Node`, one
`TokenDirectory` and one `AaveReader` per chain for the whole lookup, however
many wallets it covers. The Aave contracts are resolved once per chain, and the
rate limiter sees one stream per chain instead of three readers bursting at it.

- `adapters/chain/portfolio.py` -- the result types and the pure `total`.
- `adapters/chain/portfolio_reader.py` -- the reads, the retry and the deadline.
- `adapters/chain/portfolio_render.py` -- the answer, in English or Portuguese.

## What is read, and why nothing is counted twice

Per chain, one after another as positions are:

1. **Wallet**: `eth_getBalance` through `Node` (with its back-off), and
   `balanceOf` over the named tokens plus every Aave reserve underlying from
   `getReservesList`, in one multicall. Only underlyings are read, so an aToken
   or debt token is never a wallet balance: an Aave supply is counted once, in
   the Aave section. This is also what finds cbBTC.
2. **Liquidity**: the existing Uniswap reader. Positions are NFTs whose tokens
   sit in the pool, so they never appear as wallet balances; uncollected fees
   are included in the total and shown on the liquidity line.
3. **Aave**: the existing lending reader. The net is per asset,
   Σ (supplied − borrowed) × oracle price, not `collateral − debt`, which leaves
   out a supply not enabled as collateral.

The reserve list, with symbols and decimals, is cached per process for an hour
(`aave.ReserveCache`), keyed by chain and pool: public state, the same for
everyone, so sharing it discloses nothing.

## Prices

One source per chain -- the same asset priced twice in one answer, a tenth of a
percent apart, reads as a mistake. The chain's Aave oracle prices ether (as
WETH) and every reserve. If it does not answer, ether falls back to CoinGecko,
and the answer says so. A stablecoin the oracle does not list is $1. Anything
else unpriced is named and excluded.

## Failure

Each (chain, section) is bounded by the positions timeout and tried twice.
Everything shares one deadline (three sections × chains × timeout), so a slow
chain costs the others their retry rather than making the answer late. A
section still unread makes the headline "**At least $X** — not read: Base
(Aave)"; the figure is never presented as the total.

## Addresses

The tool takes `addresses`: one to three, space separated. The clearance
(`clearance.clear_addresses`) is `clear_address` applied to each piece: rooted
in the question or the asker's saved values, every piece an address (a text
with one word in it is refused, never trimmed), at most three, budgeted per
(tool, set of addresses).

The route decides whose: the addresses typed (or carried from the asker's own
earlier question), plus the saved wallet when the question is about the asker's
own money. A person has one saved Ethereum wallet, so "all saved wallets" is
the saved wallet plus whatever they typed. A typed address in a question that
is not first person ("what is 0x… worth in total?") is read alone.

## Routing

`portfolio_question` recognises, before retrieval:

- anchored short forms: "quanto (eu) tenho (no total)?", "quanto vale minha
  carteira?", "qual o meu saldo total?", "how much do I have (in total)?",
  "what's my portfolio/net worth (worth)?", "what's my total balance?";
- a portfolio word (portfolio, portfólio, patrimônio) with first person and a
  value word, or with an address;
- a total phrase ("in total", "no total", "total balance", "valor total",
  "net worth") with an address, or with first person and a word that makes it
  money on chain (wallet, carteira, crypto, defi);
- "e no total?" / "and in total?" after a chain question in the last three
  turns.

A conversation verb sends it to the corpus as before, and a pool or loan word
without a portfolio word leaves it to the positions route.

`app/routing_crypto.py` runs PORTFOLIO, then the positions labels, then
WALLET_BALANCE, and returns one `CryptoQuery`. The answer service answers every
label from `CHAIN_HANDLERS` (servers, the one tool, the decision name) through
one method, so a new chain route is a label, a predicate and a row. The labels
are spelled as the planned single router spells them.

## Language

The answer is rendered by the adapter, verbatim, so the language comes from
the asker's own question as the clearance carries it: `detect`, and for a
question it cannot place, the portfolio's own Portuguese words.

"quanto" and "tenho" are also added to `detect`'s Portuguese words, for every
route. The reply to "quanto eu tenho no total?" with no saved wallet is the
fixed "Which wallet?" refusal, which `AskService` localises with `detect`, not
the renderer; with neither word known the question is UNKNOWN and the refusal
went out in English. The side effect is that other short questions built on
them ("quanto custa o ETH?", "quanto tempo tenho?") are now Portuguese rather
than UNKNOWN on every route -- which is what they are.
