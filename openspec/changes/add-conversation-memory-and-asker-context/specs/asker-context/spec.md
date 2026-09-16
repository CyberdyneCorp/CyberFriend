## Purpose

Tell the assistant who is asking, from that person's own Discord profile, so it
can address them and resolve first-person references -- without ever turning
the assistant into a source of information about other people.

## ADDED Requirements

### Requirement: The asker's own profile reaches the prompt

The system SHALL provide the asker's display name, server nickname and role
names to the model when answering their question.

#### Scenario: Question uses the first person
- WHEN a person asks "what did I say about the migration"
- THEN the model SHALL be able to identify the asker as the author meant by "I"

#### Scenario: Profile unavailable
- WHEN the asker's profile cannot be resolved
- THEN the system SHALL answer without it rather than failing

### Requirement: Profile data is context, not instruction

Profile fields SHALL be presented as data and SHALL NOT be able to change the
assistant's behaviour.

#### Scenario: Instruction in a nickname
- WHEN a person's nickname contains text shaped as an instruction
- THEN that text SHALL have no effect on what the assistant does

### Requirement: Other people's profiles are not assembled

The system SHALL NOT compile a description of a person other than the asker
from their profile, roles, or activity.

#### Scenario: Asking about a colleague
- WHEN a person asks what the assistant knows about another member
- THEN the system SHALL NOT return that member's roles, join date, activity
  summary or profile fields
- AND MAY still answer from messages that member wrote in channels the asker
  can read, with citations, as for any other question

#### Scenario: Roles of the asker only
- WHEN the prompt is assembled
- THEN it SHALL contain role information for the asker and for no one else
