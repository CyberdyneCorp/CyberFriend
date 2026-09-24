## MODIFIED Requirements

### Requirement: The asker's own profile reaches the prompt

The system SHALL provide the asker's own profile to the model when answering
their question, as supplied by the asker's platform's
`AskerProfileResolver`: on Discord the display name, server nickname and role
names; on Slack the display name, and title where set; on WhatsApp the
profile name only. A field a platform does not have SHALL be omitted, never
filled from another source.

#### Scenario: Question uses the first person
- WHEN a person asks "what did I say about the migration"
- THEN the model SHALL be able to identify the asker as the author meant by
  "I"

#### Scenario: Profile unavailable
- WHEN the asker's profile cannot be resolved
- THEN the system SHALL answer without it rather than failing

#### Scenario: A platform without roles
- WHEN a WhatsApp person asks a question
- THEN the prompt SHALL carry no role names and no phone number
