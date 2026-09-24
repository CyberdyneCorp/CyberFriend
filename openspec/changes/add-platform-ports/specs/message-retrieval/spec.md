## MODIFIED Requirements

### Requirement: Verifiable citations

Every returned result SHALL identify its source well enough for a person to
open the original message on its source platform: through that platform's
permalink where it has permalinks, and otherwise by channel, author and
timestamp.

#### Scenario: Result returned
- WHEN a retrieval returns a result
- THEN the result SHALL include its channel, author, timestamp, and a link
  that resolves to the original message on its platform

#### Scenario: Delivered where links cannot open
- WHEN a result is shown on a platform or in a mode where the source
  platform's link is not offered (for example a linked identity on WhatsApp)
- THEN the citation SHALL name the channel, author and time as plain text
