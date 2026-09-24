## ADDED Requirements

### Requirement: A total is asked for in words and never answered from the corpus

The system SHALL recognise a question about what somebody holds in total, in
English or Portuguese, before retrieval, and SHALL answer it from the chain.

#### Scenario: A saved wallet and a bare question
- WHEN someone who saved a wallet asks "quanto eu tenho no total?"
- THEN their saved wallet SHALL be read on every configured chain
- AND the corpus SHALL NOT be searched

#### Scenario: No wallet at all
- WHEN someone with no saved wallet asks for their total without typing an
  address
- THEN the reply SHALL ask for an address
- AND nothing SHALL be read from a chain

#### Scenario: A follow-up
- WHEN someone asks "e no total?" or "and in total?" after a question about an
  address or their wallet in their last few questions
- THEN the total SHALL be for that address or their saved wallet

#### Scenario: A total of something else
- WHEN a question asks for a total of something that is not money on chain,
  or asks what people said about a portfolio
- THEN it SHALL be answered as it was before this capability

### Requirement: Whose wallets a total covers

A total SHALL cover the asker's saved wallet together with any address they
typed about their own money, and SHALL cover only a typed address when the
question is about that address, with at most three addresses per question.

#### Scenario: Saved and typed
- WHEN someone with a saved wallet asks for their portfolio and types a second
  address
- THEN both SHALL be read, each with its own subtotal, and summed

#### Scenario: Somebody else's address
- WHEN someone asks what a typed address is worth in total
- THEN only that address SHALL be read

#### Scenario: An address beside a word
- WHEN the addresses handed to the tool contain anything that is not an address
- THEN nothing SHALL be read

### Requirement: Nothing is counted twice or left out silently

The total SHALL be wallet balances, plus open liquidity positions with their
uncollected fees, plus the Aave net per asset, and SHALL state what it does not
include.

#### Scenario: An Aave supply
- WHEN a wallet has supplied an asset to Aave
- THEN the supply SHALL be counted once, in the Aave net
- AND its aToken SHALL NOT be read as a wallet balance

#### Scenario: A debt
- WHEN a wallet has borrowed on Aave
- THEN the debt SHALL be subtracted, per asset, at the oracle price

#### Scenario: A supply that is not collateral
- WHEN an asset is supplied but not enabled as collateral
- THEN it SHALL still be counted in the Aave net

#### Scenario: A token outside the named set
- WHEN a wallet holds an asset listed as an Aave reserve on that chain
- THEN it SHALL be read and counted

#### Scenario: Something without a price
- WHEN a holding or position has no price
- THEN it SHALL be named and SHALL NOT be counted as zero or summed

#### Scenario: What is not read
- WHEN a total is given
- THEN the answer SHALL say that other tokens, other exchanges and protocols,
  and other Aave markets are not included

### Requirement: One price source per chain

Each chain's figures SHALL be priced by that chain's Aave oracle, with a
dollar-pegged stablecoin the oracle does not list at one dollar.

#### Scenario: The oracle does not answer
- WHEN the oracle cannot be read
- THEN ether MAY be priced from the market data source
- AND the answer SHALL say so

### Requirement: A part that could not be read makes the total a lower bound

A section of a chain that could not be read SHALL be tried once more; if it
still fails, the answer SHALL give the sum of what was read as "at least" and
SHALL name the chain and section that were not read.

#### Scenario: Aave on one chain fails twice
- WHEN the Aave read on Base fails on both attempts
- THEN the headline SHALL read "at least" with the sum of everything else
- AND SHALL name Base and Aave
- AND SHALL NOT present the figure as the total

#### Scenario: A read that fails once
- WHEN a section fails and then answers on the second attempt
- THEN the total SHALL be complete

### Requirement: The answer is figures only, in the asker's language

The answer SHALL be rendered by the adapter with no model writing any figure,
per chain and, when several wallets are read, per wallet, and SHALL be written
in the language of the question.

#### Scenario: A Portuguese question
- WHEN the question is in Portuguese
- THEN the lines and number formats SHALL be Portuguese ("US$ 1.234,56")

#### Scenario: Addresses in a channel
- WHEN the answer is given in a channel
- THEN no wallet address SHALL be written in it
