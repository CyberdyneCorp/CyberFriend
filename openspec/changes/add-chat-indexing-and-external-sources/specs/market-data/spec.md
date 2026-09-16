## Purpose

Answer "what is BTC at", "where is the S&P 500" and "how much is 100 USD in
BRL" from live market data, stating how current each figure is, and never from a
price someone once mentioned in a channel.

## ADDED Requirements

### Requirement: Current prices come from market data, never the corpus

A question asking for a current price, index level or exchange rate SHALL be
answered from a market data source and SHALL NOT be answered from channel
messages.

#### Scenario: Price discussed in a channel
- WHEN a channel contains "BTC is at 60k" from last month and a person asks the
  current BTC price
- THEN the answer SHALL come from market data
- AND the channel message SHALL NOT be presented as the current price

### Requirement: Supported instruments

The system SHALL provide current prices for Bitcoin and Ether, the level of the
S&P 500, and conversion between ISO 4217 currencies.

#### Scenario: Crypto price
- WHEN a person asks for the price of BTC or ETH
- THEN the answer SHALL give the price with its currency and source

#### Scenario: Index level
- WHEN a person asks where the S&P 500 is
- THEN the answer SHALL give its level and source

#### Scenario: Currency conversion
- WHEN a person asks to convert an amount between two currencies
- THEN the answer SHALL give the converted amount and the rate used

### Requirement: Every figure states how current it is

Each figure SHALL be reported with the time it refers to, or, where the source
provides none, the time it was retrieved, labelled as such.

#### Scenario: Reference exchange rate
- WHEN a conversion uses a daily reference rate
- THEN the answer SHALL say it is a daily reference rate and give its date
- AND SHALL NOT present it as a live tradeable rate

#### Scenario: Source gives no quote time
- WHEN a source returns a level with no timestamp
- THEN the answer SHALL give the retrieval time and say that it is the
  retrieval time

### Requirement: Arguments come from closed vocabularies

Market data tools SHALL accept instruments only from a fixed list and currencies
only from ISO 4217 codes, and SHALL reject anything else before any outbound
request.

#### Scenario: Unknown instrument
- WHEN a request names an instrument outside the supported list
- THEN it SHALL be refused without contacting the provider

#### Scenario: Free text in an argument
- WHEN a currency argument contains anything other than a valid ISO 4217 code
- THEN it SHALL be refused without contacting the provider

### Requirement: Factual, not advisory

Market data answers SHALL report figures and SHALL NOT recommend buying,
selling or holding anything.

#### Scenario: Asked what to do
- WHEN a person asks whether they should buy BTC
- THEN the answer SHALL NOT make a recommendation
