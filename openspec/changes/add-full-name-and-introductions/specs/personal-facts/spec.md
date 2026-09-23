## ADDED Requirements

### Requirement: An introduction sets every fact it states

The system SHALL recognise each personal fact stated in one message and store
each of them, and SHALL reply saying what was saved and what was not.

#### Scenario: A full introduction
- WHEN someone writes their name, what to call them, their email, where they
  live and their phone in one message
- THEN the full name, preferred name, email and phone SHALL be stored
- AND the reply SHALL say where they live was not saved
- AND the message SHALL NOT be answered from the corpus or remembered as a turn

### Requirement: A full name is kept separately from the preferred name

The system SHALL store a full name as its own fact, and SHALL keep addressing the
person by their preferred name.

#### Scenario: A multi-word name
- WHEN someone says "my name is" followed by two or more words
- THEN it SHALL be stored as their full name

#### Scenario: A one-word name
- WHEN someone says "my name is" followed by one word
- THEN it SHALL be stored as their preferred name, as before

### Requirement: Contact details stated in a channel never enter the corpus

The system SHALL withhold from ingest a channel message in which the sender
states their own email address or phone number, alone or among other facts.

#### Scenario: Phone in an introduction
- WHEN a channel message introduces its sender and includes their phone number
- THEN it SHALL NOT be stored in the corpus

### Requirement: Asking what the assistant knows is recognised as typed

The system SHALL recognise "oque voce sabe sobre mim" as a request to show the
asker's facts.

#### Scenario: Joined spelling
- WHEN someone asks "oque voce sabe sobre mim?"
- THEN their stored facts SHALL be shown and the corpus SHALL NOT be searched
