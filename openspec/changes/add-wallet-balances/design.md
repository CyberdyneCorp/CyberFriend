## Which clearance rule this tool is held to

The egress guard has two rules, and picking the wrong one here would be the
whole bug.

**Not a closed vocabulary.** The market providers are checked by membership:
`ETH` may leave because it is in a fixed set of three. Addresses are a set of
size 2^160, so there is no set to be a member of, and `MarketProvider` refuses
to exist under a server name `CLOSED_VOCABULARIES` does not cover -- correctly,
because a provider that quietly fell back to rooting while looking like a
membership-checked one is exactly the confusion that table exists to prevent.

**Rooting, which turns out to be exactly right.** A web query must be made of
the asker's own words. An address is a word: `_WORD` is `[^\W_]+`, so
`0x742d35Cc...` tokenises whole, and `keep_asker_words` returns it intact when
the asker typed it and the empty string when they did not.

That gives the property this feature needs, for free and by construction: the
assistant can look up a wallet **you** typed, and cannot look up one it read in
a channel. Without it, anyone who could ask the bot a question could use it to
sweep every address mentioned across the channels they can read -- which is a
worse capability than it sounds, because the bot can read more channels than
most individuals.

So the provider follows `adapters/web`'s clearance shape, not
`adapters/market`'s, even though the question is a market-ish one.

Rooting alone is not sufficient, because it admits any word the asker typed.
A structural gate follows it: the argument must match a 20-byte hex address, or
the call is refused before any request. Refused, never trimmed -- trimming an
address into "whatever part of it was valid hex" is how you send a request for
somebody else's wallet.

## Why the token list is named rather than discovered

The endpoint available to this deployment is Infura, and JSON-RPC cannot
enumerate what an address holds: `eth_getBalance` gives the native balance, and
a token balance requires knowing the contract to call `balanceOf` on. Full
discovery needs a provider with an indexed view.

Rather than pretend otherwise, the set is explicit per chain and a token is
reported because this deployment named it. A zero balance is omitted, so an
address holding none of them reads as "no tokens I know about" rather than a
wall of zeros. The honest limit is stated in the answer and in the docs: a token
nobody listed is invisible, not absent.

## Read-only by construction, not by intention

`eth_getBalance` and `eth_call` are the only verbs. There is no signer, no
private key and no mnemonic anywhere in this package, so there is nothing that
could sign a transaction even if some future code asked it to. That is the
difference between a read-only tool and a tool that currently only reads.

This matters more than usual here: the federation layer has a whole confirmation
apparatus for state-changing tools, and the right way to use it is to never
need it. The allowlist entry declares `READ_ONLY` and `mutation_enabled=False`,
which is what the registry accepts that determination from.

## Two chains, reported separately

Ethereum and Base are read independently and reported independently. One
endpoint failing must not turn into "this address holds nothing", which is the
failure that would actually mislead somebody: an empty answer and an
unreachable answer look identical once they are prose. A chain that could not
be read is named as such, and the chain that could is still reported.
