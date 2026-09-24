## ADDED Requirements

### Requirement: A total is the portfolio's, and a bare balance is the saved wallet's

A question asking for a total SHALL be routed to the portfolio before the
wallet balance route, and a question that is only "what is my balance?" SHALL
be a wallet question about the asker's saved wallet.

#### Scenario: A total balance
- WHEN someone asks "what's my wallet's total balance?"
- THEN it SHALL be answered as a portfolio total, not as balances

#### Scenario: My balance
- WHEN someone who saved a wallet asks "what is my balance?"
- THEN their saved wallet's balances SHALL be read
- AND the corpus SHALL NOT be searched

#### Scenario: A balance of something else
- WHEN a longer question asks about a balance without naming a wallet, such as
  "my balance of vacation days"
- THEN it SHALL be answered from the corpus as before

### Requirement: A rate-limited balance read is retried

A balance read that the endpoint rejects as rate limited SHALL be retried with
back-off before the chain is reported unreadable.

#### Scenario: One entry of the batch is rate limited
- WHEN the endpoint answers the balance batch with a rate-limit error
- THEN the read SHALL be retried
- AND a retry that answers SHALL be reported as the chain's balances
