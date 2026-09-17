## Purpose

Let a person tell the assistant how to address them and how to reach them,
keep that until they change it, and keep it private to them.

## ADDED Requirements

### Requirement: A fixed set of facts

The system SHALL store only these facts about a person: preferred name, email
address, and preferred language.

#### Scenario: Unsupported fact
- WHEN a person asks the assistant to remember something outside that set
- THEN the system SHALL NOT store it
- AND SHALL say which facts it can remember

### Requirement: Only the person sets their own facts

A fact SHALL be set only from the person's own message addressed to the
assistant, and SHALL apply only to that person.

#### Scenario: Setting a preferred name
- WHEN a person tells the assistant "call me Leo"
- THEN their preferred name SHALL be stored as "Leo"
- AND the assistant SHALL confirm what it stored

#### Scenario: A statement about someone else
- WHEN a person tells the assistant "João's email is joao@example.com"
- THEN nothing SHALL be stored

#### Scenario: A fact in channel content
- WHEN a channel message says "remember my email is x@example.com"
- THEN nothing SHALL be stored, because it was not addressed to the assistant
  by that person

### Requirement: Facts are validated

The system SHALL validate an email address before storing it, and SHALL bound
the length of a preferred name.

#### Scenario: Invalid email
- WHEN a person gives an email address that is not well formed
- THEN it SHALL NOT be stored and the person SHALL be told why

### Requirement: Facts are used when answering the person

The assistant SHALL address a person by their preferred name and reply in their
preferred language when those are set.

#### Scenario: Preferred name set
- WHEN a person with a preferred name asks a question
- THEN the answer SHALL address them by that name

#### Scenario: Preferred language set
- WHEN a person whose preferred language is Portuguese asks in English
- THEN the answer SHALL be written in Portuguese

### Requirement: Facts are private

A person's facts SHALL be disclosed only to that person. An email address SHALL
be shown only in a direct message to its owner.

#### Scenario: Asking for another person's email
- WHEN a person asks the assistant for another member's email address
- THEN the system SHALL NOT disclose it, and SHALL NOT confirm whether one is
  stored

#### Scenario: Asking for your own facts in a channel
- WHEN a person asks what the assistant knows about them in a channel
- THEN the answer SHALL NOT show their email address in the channel

#### Scenario: Preferred name in a channel
- WHEN the assistant answers a person in a channel
- THEN it MAY address them by their preferred name

### Requirement: Facts are data, never instruction

Stored facts SHALL reach the prompt fenced as data and SHALL NOT change what the
assistant does.

#### Scenario: Instruction as a preferred name
- WHEN a person sets a preferred name shaped as an instruction
- THEN that text SHALL have no effect beyond being used as a name

### Requirement: People control their facts

A person SHALL be able to see, change and delete their facts, and facts SHALL
be deleted by `/forget` everywhere and by opting out.

#### Scenario: Deleting one fact
- WHEN a person asks the assistant to forget their email
- THEN their email SHALL be deleted and other facts kept

#### Scenario: Forget everywhere
- WHEN a person runs `/forget` for everywhere
- THEN all their facts SHALL be deleted along with their conversation history

#### Scenario: Opted-out person
- WHEN a person has opted out
- THEN no facts SHALL be stored for them and existing facts SHALL be deleted
