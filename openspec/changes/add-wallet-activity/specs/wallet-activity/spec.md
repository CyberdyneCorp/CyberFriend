## ADDED Requirements

### Requirement: Activity is asked for in words and never answered from the corpus

The system SHALL recognise a question about what a wallet did, in English or
Portuguese, before retrieval, and SHALL answer it from the chain explorer.

#### Scenario: A saved wallet in a DM
- WHEN someone who saved one wallet asks "o que minha carteira fez essa semana?"
- THEN their saved wallet's activity SHALL be read on every configured chain
- AND the corpus SHALL NOT be searched

#### Scenario: Several saved wallets
- WHEN someone with several saved wallets asks about their wallet's activity
  without naming one
- THEN the reply SHALL ask which, and nothing SHALL be read

#### Scenario: A question about the conversation
- WHEN someone asks "what did people say about my wallet" or "what did John do
  this week?"
- THEN it SHALL be answered as it was before this capability

#### Scenario: This wallet, after a balance question
- WHEN someone asks about an address's balance and then "o que essa carteira
  fez essa semana?"
- THEN that address's activity SHALL be read, not the asker's saved wallet

#### Scenario: A period follow-up
- WHEN the asker's previous question was about a wallet's activity and they ask
  "e ontem?"
- THEN the same wallet's activity for that period SHALL be read
- AND a follow-up that names anything besides a period ("and the gas today?"),
  or follows a question that was not about activity, SHALL NOT be

#### Scenario: Fees, throughput and alerts
- WHEN someone asks about a transaction fee, a chain's transactions per second,
  or an alert on their wallet
- THEN it SHALL NOT be answered as the wallet's activity

### Requirement: The window is the asker's

The window SHALL be read from the asker's question, SHALL default to the last
seven days, SHALL be at most thirty days, and SHALL be stated in the answer.
No tool argument SHALL change it.

#### Scenario: A named period
- WHEN someone asks about "ontem"
- THEN only yesterday, in UTC, SHALL be read

#### Scenario: More than thirty days
- WHEN someone asks about the last ninety days
- THEN thirty days SHALL be read and the answer SHALL say the window was limited

#### Scenario: A model-supplied window
- WHEN the tool call carries arguments besides the address
- THEN they SHALL be ignored

### Requirement: Relayed actions are read

Activity SHALL be read from a source that includes transactions submitted by
others on the wallet's behalf, and internal and token transfers.

#### Scenario: An EIP-7702 wallet
- WHEN a relayer submitted an Aave supply for the wallet
- THEN the supply SHALL be listed, in the underlying asset

#### Scenario: A relayer's cut
- WHEN the relayed transaction also paid a plain address
- THEN that payment SHALL be listed as a transfer and SHALL NOT be called a fee

### Requirement: Spam and poisoning are hidden and counted

Transfers of tokens this deployment does not recognise in transactions the
wallet did not send, zero-value transfers, and third-party deposits worth under
one cent SHALL be hidden and counted, without relying on the explorer's
reputation labels, and the answer SHALL warn when a hidden transfer's
counterparty imitates a real one.

#### Scenario: A homoglyph token sent to a lookalike
- WHEN a fake "USDC" moves from the wallet to an address sharing the first and
  last four characters of a real counterparty
- THEN it SHALL NOT be listed
- AND the answer SHALL count it and warn about address poisoning

#### Scenario: An explorer-supplied name with markdown
- WHEN a token symbol or method name from the explorer is not plain letters,
  digits and a few symbols
- THEN it SHALL NOT be written into the answer, and the token SHALL be called
  an unlisted token

### Requirement: Counterparties follow the audience

In an answer read by the asker alone every counterparty SHALL be written in
full and never shortened; in an answer posted to a channel no address SHALL be
written in full, counterparties SHALL be described as an external address, and
the wallet SHALL be named by its last four characters.

#### Scenario: Asked in a channel
- WHEN someone asks about their wallet's activity in a channel
- THEN no full address SHALL appear anywhere in the reply, its citation included

#### Scenario: A clearance that does not say
- WHEN a provider is reached with a clearance that does not state the audience
- THEN it SHALL write the answer as for a channel

### Requirement: A chain that cannot be read is said

A chain whose explorer cannot be read SHALL be reported as unread and SHALL
NOT be reported as having no activity; more activity than the page cap SHALL be
stated.

#### Scenario: The explorer is down
- WHEN Arbitrum's explorer answers with an error
- THEN the answer SHALL say Arbitrum could not be read
