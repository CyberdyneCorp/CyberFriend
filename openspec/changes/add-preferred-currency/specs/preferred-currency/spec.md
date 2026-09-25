## ADDED Requirements

### Requirement: Dollar figures are also shown in the preferred currency

When the asker has a preferred currency other than US dollars, the system SHALL
follow every US dollar figure in a crypto price, wallet balance, liquidity or
lending position, portfolio total and wallet activity answer with the same
amount in that currency, written in the reader's notation, and SHALL name the
rate used. The conversion SHALL be computed in code; a market answer SHALL
carry the converted figure in the tool result, and no model SHALL be asked to
convert a figure.

#### Scenario: A price in reais
- WHEN a person whose preferred currency is BRL asks "qual o preço do bitcoin?"
- THEN the answer SHALL show the dollar price followed by the price in reais
  ("63,210 USD (R$ 345.126,60)") and the rate's footnote

#### Scenario: A portfolio in reais
- WHEN that person asks "quanto eu tenho no total?"
- THEN every total, chain and section value SHALL show both figures

#### Scenario: Dollars as the preference
- WHEN the preferred currency is USD, or none is saved
- THEN no second figure SHALL be shown and no rate SHALL be read

### Requirement: The rate is read once, and its absence is dollars only

The system SHALL read the USD rate of the preferred currency from the market FX
provider's host through the process's HTTP transport, sending only the two
currency codes, both members of the supported set, and SHALL cache it
in-process for ten minutes. When the rate cannot be read, the system SHALL
answer in US dollars only, and SHALL NOT fail the answer or use any other rate.

#### Scenario: The rate host is down
- WHEN the FX host answers with an error or does not answer
- THEN the answer SHALL be the dollar-only answer, with no second figure

#### Scenario: Several figures in one answer
- WHEN one answer shows many dollar figures
- THEN at most one rate request SHALL be made for it

### Requirement: Alerts show the owner's preferred currency

When a price or health alert fires, the system SHALL read the owner's stored
preferred currency and show the current price, collateral and debt in it beside
the dollar figures. The level a price alert was set at SHALL stay in dollars.

#### Scenario: A price alert for a person who prefers reais
- WHEN a BTC price alert fires for a person whose preferred currency is BRL
- THEN the message SHALL show the price in dollars followed by the price in reais
