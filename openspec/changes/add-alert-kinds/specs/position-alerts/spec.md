## ADDED Requirements

### Requirement: A person can be told when BTC or ETH crosses a price

A person SHALL be able to ask, in English or Portuguese, to be told when the
BTC or ETH price goes above or below a level in US dollars. The assets SHALL be
exactly the market tools' closed crypto vocabulary. A price alert SHALL watch
no wallet: it SHALL store no chain and no address. The level SHALL be read in
either notation ("100k", "2.500", "2,500", "$3,000"), and a level with no
direction SHALL be taken as the side the price is not on when the alert is made.

#### Scenario: A request in Portuguese
- WHEN a person writes "avisa quando o BTC passar de 100k" and BTC is at 97,412
- THEN the reply SHALL offer an alert for BTC above US$ 100.000 with the price
  now and its quote time, in Portuguese, with Confirm and Cancel
- AND no chain endpoint SHALL be read

#### Scenario: A price asked, not a watch
- WHEN a person asks "what is the BTC price?" or "quanto está o ETH?"
- THEN no alert SHALL be offered

#### Scenario: A question about a past price
- WHEN a person asks "tell me when BTC first went above 100k", "can you tell me
  when bitcoin crossed 100k?" or "me diga quando o BTC passou de 100k"
- THEN no alert SHALL be offered and the question SHALL be answered as one

#### Scenario: A number that is not the price
- WHEN a person asks to be told when the gas on ethereum drops below 20 gwei,
  or when BTC dominance goes above 60%
- THEN no price alert SHALL be offered

#### Scenario: The direction nearest the level
- WHEN a person writes "alert me if ETH over the next week drops below 2500"
- THEN the alert offered SHALL be for ETH below US$ 2,500

#### Scenario: No level
- WHEN a person asks to be told when BTC rises, with no level
- THEN the reply SHALL ask for the level and SHALL read nothing

### Requirement: A price alert fires on the crossing, once

A price alert SHALL message when the price reaches or passes its level in the
direction asked, and SHALL NOT message again until the price has gone back past
the level by 0.5% of it and crossed again. Going back SHALL NOT message.

#### Scenario: Crossing up
- WHEN an alert for BTC above 100,000 was below and a check reads 100,412
- THEN one message SHALL be sent in the alert's language with the price and its time

#### Scenario: Wobbling on the level
- WHEN the price then reads 99,800 and 100,100 on the next checks
- THEN no further message SHALL be sent

### Requirement: Price checks send one constant request

All price alerts due in a check SHALL be read from one request to the market
tools' crypto price source, and that request SHALL be constant: it SHALL NOT
vary with the assets, levels or people watched. No request SHALL be sent when
no price alert is due, and a check without a fresh price SHALL change nothing
and send nothing.

#### Scenario: Several price alerts
- WHEN alerts on BTC and ETH are due in the same check
- THEN exactly one request SHALL be sent for both

#### Scenario: The source is down
- WHEN the price source does not answer
- THEN each price alert SHALL count a failed check and no message SHALL be sent

#### Scenario: One coin missing from the answer
- WHEN the price source answers with ETH but no usable BTC price
- THEN the ETH alerts SHALL be checked and only the BTC alerts SHALL count a
  failed check

### Requirement: A range alert can warn near its edge

A range alert MAY carry a distance between 1% and 50% from the range's edge.
The distance SHALL be measured on the price as the range is quoted, to the
nearer bound. The alert SHALL message once when a position in range comes
within that distance, SHALL re-arm silently one percentage point further back,
and SHALL still message on leaving the range and coming back. Asking for a
distance on a position already watched SHALL add it to that alert rather than
create a second, and SHALL NOT need a free slot under the cap. A plain range
request for a position that already has a near-edge warning SHALL be answered
as already watched.

#### Scenario: The confirmation shows the distance
- WHEN a person asks "warn me when my LP is within 3% of the range edge" and
  Arbitrum position #210171 is at tick -197404 in [-197920, -197070)
- THEN the confirmation SHALL say the position is 3.4% from the upper edge

#### Scenario: Coming near the edge
- WHEN a position with a 5% warning reads within 5% of an edge on two
  consecutive checks
- THEN one message SHALL be sent naming the edge and the distance

#### Scenario: A distance on a watched position at the cap
- WHEN a person with ten active alerts, one of them a range alert on #210171,
  asks for a 3% warning on #210171
- THEN the warning SHALL be offered and, on Confirm, added to that alert

#### Scenario: A distance out of bounds
- WHEN a person asks for a warning at 60% from the edge
- THEN the reply SHALL give the bounds and nothing SHALL be offered

### Requirement: Every kind counts against one cap and one listing

The cap of ten active alerts SHALL count every kind together, and `/alert
list` SHALL show price alerts with their level and last price, and range
alerts with their edge distance.

#### Scenario: At the cap with mixed kinds
- WHEN a person with ten active alerts of any kinds asks for a price alert
- THEN the reply SHALL say they are at the cap and nothing SHALL be read

### Requirement: Rolling back removes what the old schema cannot hold

Downgrading the schema past the migration that adds these kinds SHALL delete
price alerts and fold the near-edge state back into "in range" before the
columns and constraints are restored.

#### Scenario: Downgrade
- WHEN the schema is downgraded with a price alert and a near-edge alert stored
- THEN the price alert SHALL be deleted and the range alert kept, in range
