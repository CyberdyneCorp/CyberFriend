# add-defi-positions

Report a wallet's Uniswap v3/v4 liquidity positions and its Aave v3 supplies
and borrows, on Ethereum, Base and Arbitrum.

Same clearance as `add-wallet-balances`: the address must be one the asker typed
or saved about themselves. Everything is read with `eth_call`; nothing here can
sign.

- `proposal.md` -- why, what changes, and the risk
- `design.md` -- how positions are found, valued and bounded
- `specs/defi-positions/spec.md` -- the requirements
- `tasks.md` -- progress
