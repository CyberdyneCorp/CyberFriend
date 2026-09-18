# add-wallet-balances

Report what a `0x` address holds on Ethereum and Base -- native ETH and a named
set of ERC-20 tokens -- with USD values from the existing price source.

The decision that shapes everything else is which egress rule applies. Not a
closed vocabulary: addresses have no fixed set to be a member of. Rooting, which
turns out to be exactly right, because an address is a word and
`keep_asker_words` returns it intact when the asker typed it and empty when they
did not. The assistant can look up a wallet **you** typed and cannot look up one
it read in a channel.

- `proposal.md` -- why, what changes, and the risk
- `design.md` -- the clearance rule, and why the token set is named
- `specs/wallet-balances/spec.md` -- the requirements
- `tasks.md` -- progress
