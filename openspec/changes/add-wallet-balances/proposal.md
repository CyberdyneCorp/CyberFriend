## Why

The assistant can say what ether is worth and cannot say how much of it you
have. "What is in this wallet" is the question people actually ask next, and it
is the one question where the answer has to come from a chain rather than from
anything anybody said in a channel.

## What Changes

- **Wallet balances.** Given an address, report its native balance on Ethereum
  and Base, plus the balances of a known set of ERC-20 tokens, each with its
  USD value from the existing price source.
- **Only an address the asker typed.** The lookup is held to the same rooting
  rule as a web query, so an address that appears in retrieved content cannot
  become one.
- **Read-only, structurally.** The provider issues `eth_getBalance` and
  `eth_call` and has no other verb.

Non-goals:

- **Signing or sending anything.** No transaction, no approval, no contract
  deployment. The provider holds no key that could sign one.
- **Discovering which tokens an address holds.** The RPC endpoint cannot
  enumerate holdings; a token is reported because this deployment names its
  contract, and a balance of zero is not reported at all.
- **Transaction history, NFTs, DeFi positions.** A balance is a balance.
- **Naming a wallet.** No ENS resolution, and no association of an address with
  a person.

## Capabilities

### New Capabilities

- `wallet-balances`: what an address holds, on which chains, and the rule about
  whose words an address may be.

### Modified Capabilities

None. The tool is registered, routed, cleared and audited by the federation
layer exactly as the web and market tools are.

## Risk

An address is not secret, but *which* addresses this deployment is asked about
is a signal, and it leaves in a request to a third-party RPC endpoint. The
rooting rule is what bounds it: only an address a person typed into their own
question is ever sent, so the assistant cannot be used to sweep the addresses
mentioned across the channels somebody can read.
