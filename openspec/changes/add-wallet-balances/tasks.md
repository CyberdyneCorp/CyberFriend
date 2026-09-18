## 1. The clearance rule

- [x] 1.1 A provider server name held to rooting, not to a closed vocabulary
- [x] 1.2 Structural address gate, refusing rather than trimming
- [x] 1.3 Test: an address the asker typed is admitted
- [x] 1.4 Test: an address only present in retrieved content is refused
- [x] 1.5 Test: a rooted word that is not an address is refused before any request

## 2. Reading the chains

- [x] 2.1 JSON-RPC client over the configured endpoints, bounded and read-only
- [x] 2.2 Native balance per chain via `eth_getBalance`
- [x] 2.3 Token balances via `eth_call` `balanceOf`, over a named per-chain set
- [x] 2.4 Omit a zero balance; state that the token set is named, not discovered
- [x] 2.5 Report an unreachable chain as unreachable, never as empty
- [x] 2.6 Test: one chain failing still reports the other

## 3. Presenting it

- [x] 3.1 USD values from the existing market price source, with their age
- [x] 3.2 Discord markdown, per chain, figures only and no recommendation
- [x] 3.3 Route a wallet question to this tool
- [x] 3.4 Test: asked what to do with a balance, no recommendation is made

## 4. Settings, wiring and documentation

- [x] 4.1 Settings for enablement, endpoints and timeout
- [x] 4.2 Declare every setting in `docker-compose.yml`
- [x] 4.3 Wire into federation in `composition.py` and assert the call chain
- [x] 4.4 README and docs: what is read, and that the token set is named

## 5. Live

- [x] 5.1 Deploy with the endpoints configured
- [x] 5.2 Confirm the provider answers for a real address against live chains
- [x] 5.3 Route a wallet question outside the corpus before retrieval
- [ ] 5.4 Confirm an address asked in Discord answers

5.3 was a defect found in Discord: the tool was registered, offered, read-only
and never called, because the corpus answered first from a colleague's message
about a different project and the critic judged it sufficient.

5.2 was verified by running the guarded provider against Ethereum and Base:
the clearance was minted, the egress log recorded `closed_vocabulary=False`
(rooting, as designed), and both chains returned native and token balances with
USD values. `chain_balances:wallet_balances` is registered and read-only in the
deployed bot. 5.3 is the Discord leg, and needs somebody to ask.
