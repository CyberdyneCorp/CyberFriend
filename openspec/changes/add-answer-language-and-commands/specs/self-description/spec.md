## Purpose

Say what this deployment can actually do, from what it is running, in the
language the question was asked in.

## ADDED Requirements

### Requirement: Capabilities come from configuration, never from the corpus

The system SHALL answer a question about itself from what it is running, and
SHALL NOT search channel content for it.

#### Scenario: Asked what it can do
- WHEN someone asks what the assistant can do
- THEN the answer SHALL be assembled from the running configuration
- AND the corpus SHALL NOT be searched

#### Scenario: A channel describes another product
- WHEN a channel contains a message describing some other tool's features
- THEN those features SHALL NOT be reported as the assistant's own

### Requirement: Every command is listed

The reply SHALL name each command the assistant offers and what it does.

#### Scenario: Asked what it can do
- WHEN someone asks what the assistant can do or which commands exist
- THEN every available command SHALL be listed

#### Scenario: A command that is not available
- WHEN a command's feature is switched off for this deployment
- THEN that command SHALL NOT be listed

#### Scenario: Asked in a direct message
- WHEN someone asks what the assistant can do in a direct message
- THEN a command registered only on the server, which Discord does not list
  in a direct message, SHALL NOT be listed

### Requirement: Capabilities are named in human terms

The reply SHALL describe what a capability does rather than naming the server
that provides it.

#### Scenario: An external source is registered
- WHEN an external source is registered
- THEN the reply SHALL say what it is for
- AND SHALL NOT present an internal identifier as the capability's name

### Requirement: The reply is in the language of the question

The reply SHALL be written in the language the question was asked in.

#### Scenario: Asked in Portuguese
- WHEN someone asks in Portuguese what the assistant can do
- THEN the reply SHALL be in Portuguese

#### Scenario: Asked in English
- WHEN someone asks in English what the assistant can do
- THEN the reply SHALL be in English

### Requirement: Every running feature is described, and only those

The reply SHALL describe each feature this deployment runs, with an example
question in the reply's language, and SHALL NOT describe a feature that is
switched off. It SHALL cover: questions about channels with citations,
catch-up, what someone said, decisions, what was asked of the asker (where ask
extraction is on), personal facts (where the process keeps them: preferred and
full name, email, phone, home address, birth date, preferred language, several
ETH and BTC wallets, several facts in one message, showing and forgetting
them), wallet balances, Uniswap v3/v4 positions, Aave supplies, borrows and
health factor, the portfolio total and wallet activity (where the wallet tools
are registered), market prices (per registered market source), alerts and
`/alert list|delete` (where alerts are available), scheduled questions (where
they are on), voice questions in a direct message (where they are on), web and
other external lookups (where registered), and the audience rule.

#### Scenario: Everything on, asked in Portuguese
- WHEN a deployment with wallet tools, alerts and voice on is asked "o que você
  pode fazer?" in a direct message
- THEN the reply SHALL list the crypto, alerts, decisions and voice sections
  in Portuguese, each with an example question

#### Scenario: A feature switched off
- WHEN a feature's setting is off, or the tool it needs is not registered
  (such as wallet tools without an Infura key)
- THEN the reply SHALL NOT mention that feature

#### Scenario: A bare mention
- WHEN someone mentions the assistant with no question
- THEN the reply SHALL be the same description, in the person's saved
  language, from the same configuration

### Requirement: The reply fits Discord

The reply SHALL be laid out as short sections separated by blank lines, SHALL
be answered without a model call, and SHALL be deliverable as messages of at
most 2000 characters each, with no section split across two messages.

#### Scenario: Everything on
- WHEN every feature is on
- THEN each message sent SHALL be at most 2000 characters
- AND each section SHALL arrive whole in one message
