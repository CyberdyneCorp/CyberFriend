## ADDED Requirements

### Requirement: A chain question without an address uses the one the asker just gave

The system SHALL, when a chain question (balance, positions, portfolio,
activity or alert) needs an address and none is typed, carried or saved, use
the latest address the asker typed in their own remembered questions in the
same conversation location, and SHALL never take one from another person's
message, from retrieved content or from another location. A message that only
points back at a wallet SHALL ask the asker's latest chain question again with
that address.

#### Scenario: A DM follow-up
- WHEN, in a DM, "verifique as pools de liquidez nesta wallet 0x…" was answered
  and the asker then writes "Porque você não respondeu o valor das pools na
  minha moeda de base?" or "A carteira que eu acabei de passar"
- THEN that address SHALL be read, and the reply SHALL NOT ask which wallet

#### Scenario: A channel follow-up
- WHEN the asker typed the address in the same channel conversation
- THEN a follow-up there SHALL read it

#### Scenario: Somebody else's address
- WHEN an address appears only in another person's message, in retrieved
  content, or in the asker's DM rather than this channel
- THEN it SHALL NOT be read, and the reply SHALL ask for an address
