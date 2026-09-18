## Purpose

Report what an address holds on Ethereum and Base, without the assistant ever
being able to look up an address the person asking did not type.

## ADDED Requirements

### Requirement: Only an address the asker typed is looked up

The system SHALL send an address to a chain endpoint only when that address
appears in the asker's own question, and SHALL refuse otherwise.

#### Scenario: The asker types an address
- WHEN someone asks for the balance of an address they wrote in their question
- THEN that address MAY be sent to the chain endpoint

#### Scenario: An address appears in retrieved content
- WHEN an address appears in a message, document or tool result rather than in
  the question
- THEN it SHALL NOT be sent
- AND the refusal SHALL NOT be explained to the requester in terms of the
  address

### Requirement: Anything that is not an address is refused before it is sent

The system SHALL check that an argument is a well-formed address before any
request, and SHALL refuse it outright rather than trimming it.

#### Scenario: A malformed address
- WHEN the argument is not a 20-byte hexadecimal address
- THEN no request SHALL be made

#### Scenario: A word that merely survived rooting
- WHEN a rooted argument is not an address
- THEN no request SHALL be made

### Requirement: The provider can only read

The system SHALL issue only balance-reading calls, and SHALL hold no key
capable of authorising a state change.

#### Scenario: Any wallet question
- WHEN a wallet balance is looked up
- THEN only read calls SHALL be issued
- AND no transaction SHALL be signed, sent or approved

### Requirement: Balances are reported per chain, with their source and age

The system SHALL report each balance against the chain it was read from and
SHALL state how current the figure is.

#### Scenario: An address with holdings on both chains
- WHEN an address holds assets on Ethereum and on Base
- THEN each balance SHALL be attributed to its chain

#### Scenario: A token worth nothing is held
- WHEN a known token's balance is zero
- THEN it SHALL NOT be listed

#### Scenario: A USD value is shown
- WHEN a balance is converted to USD
- THEN the price SHALL come from the existing market source
- AND the answer SHALL state how current the price is

### Requirement: A chain that cannot be reached is reported, not invented

The system SHALL distinguish a chain it could not read from an address that
holds nothing there.

#### Scenario: One endpoint is unavailable
- WHEN one chain's endpoint does not answer
- THEN the answer SHALL say that chain could not be read
- AND SHALL still report the chain that could

#### Scenario: No endpoint is configured
- WHEN no chain endpoint is configured
- THEN the tool SHALL NOT be offered at all

### Requirement: The assistant reports figures and does not advise

The system SHALL NOT recommend buying, selling, holding or moving anything.

#### Scenario: Asked what to do with a balance
- WHEN someone asks whether to sell what an address holds
- THEN the figures SHALL be reported without a recommendation
