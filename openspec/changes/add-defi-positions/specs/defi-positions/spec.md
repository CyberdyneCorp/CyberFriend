## Purpose

Report an address's liquidity and lending positions from the chain, without the
assistant ever being able to look up an address the asker did not give it.

## ADDED Requirements

### Requirement: Only the asker's address is looked up

The system SHALL look up positions only for an address in the asker's question
or saved by the asker as their own wallet, under the same clearance as wallet
balances.

#### Scenario: The asker's own address
- WHEN someone asks about the positions of an address they typed
- THEN that address MAY be looked up

#### Scenario: An address from retrieved content
- WHEN an address appears only in retrieved content
- THEN no request SHALL be made for it

### Requirement: Liquidity positions are listed with their state

The system SHALL list open Uniswap v3 and v4 positions of the address on
Ethereum, Base and Arbitrum, each with pair and fee tier, amounts held and USD
value, in-range status, minimum and maximum price with the current price, and
uncollected fees.

#### Scenario: Closed positions
- WHEN a position NFT has zero liquidity
- THEN it SHALL NOT be listed or counted, even with fees left uncollected

#### Scenario: Out of range
- WHEN the pool's current tick is outside the position's range
- THEN the position SHALL be marked out of range

#### Scenario: A v4 position the explorer has not indexed yet
- WHEN the chain reports more v4 positions than the explorer returns
- THEN the chain's history SHALL be searched for the block each missing position arrived in
- AND each position found SHALL still be confirmed on-chain before it is listed

#### Scenario: A v4 position the chain does not confirm
- WHEN the explorer returns a token ID the chain says the address does not own
- THEN it SHALL NOT be listed

### Requirement: Aave positions are listed

The system SHALL list the address's Aave v3 supplied and borrowed assets per
chain with amounts, USD values and rates, and the account's collateral, debt
and health factor.

#### Scenario: No debt
- WHEN the account has no debt
- THEN the health factor SHALL be reported as not applicable rather than as a
  number

### Requirement: Failure is reported, never shown as empty

The system SHALL report a chain or source it could not read as unreadable, and
SHALL NOT present it as holding no positions.

#### Scenario: One chain unreachable
- WHEN one chain fails
- THEN the others SHALL still be reported
- AND the failed chain SHALL be named as unreadable

### Requirement: Read-only

The system SHALL read positions with `eth_call` only and SHALL NOT sign or send
any transaction.

#### Scenario: Uncollected fees
- WHEN uncollected fees are read
- THEN `collect` SHALL only be simulated with `eth_call`

### Requirement: A positions question is not answered from the corpus

The system SHALL route a question about the asker's liquidity or lending
positions to these tools before retrieval.

#### Scenario: Asked about their pools
- WHEN someone asks about their liquidity pools or Aave loans
- THEN retrieval SHALL NOT run

#### Scenario: A follow-up that names no address
- WHEN a question names a protocol or pool term but no address and no "my"
- AND one of the asker's last three questions was about a named address
- THEN that address SHALL be looked up
- AND a question with only a generic word such as "position" SHALL NOT carry it

#### Scenario: A conversation about pools
- WHEN someone asks what was said or decided about a pool
- THEN the question SHALL stay with the corpus
