## MODIFIED Requirements

### Requirement: A fixed set of facts

The system SHALL store only these facts about a person: preferred name, email
address, preferred language, phone number, Ethereum-compatible wallet address,
and Bitcoin wallet address.

#### Scenario: Unsupported fact
- WHEN a person asks the assistant to remember something outside that set
- THEN the system SHALL NOT store it
- AND SHALL say which facts it can remember

#### Scenario: Setting a wallet
- WHEN a person tells the assistant an address is their wallet
- THEN it SHALL be stored against them
- AND the assistant SHALL confirm what it stored

### Requirement: Facts are validated

The system SHALL validate each fact for the shape its kind requires before
storing it, and SHALL say why when it refuses.

#### Scenario: Invalid email
- WHEN a person gives an email address that is not well formed
- THEN it SHALL NOT be stored and the person SHALL be told why

#### Scenario: A wallet address that is not one
- WHEN a value is not a well-formed address for the chain it is offered as
- THEN it SHALL NOT be stored
- AND the person SHALL be told what shape was expected

#### Scenario: A phone number that is not one
- WHEN a phone number contains no plausible sequence of digits
- THEN it SHALL NOT be stored

### Requirement: Facts are private

A fact SHALL be shown only to the person it belongs to, and contact details
SHALL be shown only in a direct message to them.

#### Scenario: Asked in a channel what it knows
- WHEN a person asks in a channel what the assistant knows about them
- THEN their email, phone and wallet addresses SHALL NOT be shown there

#### Scenario: Asked in a direct message
- WHEN the same person asks in a direct message
- THEN their own facts SHALL be shown to them

#### Scenario: Somebody else's facts
- WHEN a person asks what the assistant knows about another person
- THEN no fact of that person SHALL be disclosed

## ADDED Requirements

### Requirement: A person's own saved value may be used for their own lookup

A value a person set about themselves SHALL be usable as an argument to an
outbound lookup made on their behalf, and SHALL be treated as their own words
for that purpose.

#### Scenario: Asking about their own balance
- WHEN a person who has saved a wallet asks what their balance is
- THEN the saved address SHALL be used for the lookup

#### Scenario: No saved wallet
- WHEN a person without a saved wallet asks what their balance is
- THEN the assistant SHALL ask for an address
- AND SHALL NOT use anybody else's

#### Scenario: Another person's saved value
- WHEN a lookup is made for one person
- THEN only values that person set SHALL be available to it

#### Scenario: A value from retrieved content
- WHEN an address appears in a message rather than in a person's own facts
- THEN it SHALL NOT be usable as an outbound argument
