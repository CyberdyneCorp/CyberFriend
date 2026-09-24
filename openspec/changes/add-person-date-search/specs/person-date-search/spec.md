## ADDED Requirements

### Requirement: Time spans are read in the deployment's calendar

The system SHALL read a time span named in a question, in Portuguese or
English and regardless of accents, as half-open UTC bounds computed from local
midnights in the zone `ANSWER_TIMEZONE` (default `America/Sao_Paulo`), without
a model call.

#### Scenario: Yesterday late in the evening
- WHEN a message was sent at 23:30 in Sao Paulo and the next day someone asks
  about "ontem"
- THEN the message SHALL fall inside the span, although it is already the
  following day in UTC

#### Scenario: Last week is the previous calendar week
- WHEN someone asks about "semana passada" or "last week" on any day
- THEN the span SHALL run from the Monday of the previous week to the Monday
  of the current week

#### Scenario: A month not yet reached
- WHEN someone asks about "em dezembro" in September
- THEN the span SHALL be December of the previous year

#### Scenario: Since
- WHEN someone asks about "desde segunda" or "since yesterday"
- THEN the span SHALL start at that day's local midnight and end now

#### Scenario: No day named
- WHEN a question names no span, or a day that does not exist such as "31/02"
- THEN no span SHALL be read

#### Scenario: A fraction is not a date
- WHEN a question mentions "1/2 ETH" or "3/4 of the quorum" with no date cue
- THEN no span SHALL be read

#### Scenario: Two spans or a range
- WHEN a question names "hoje e ontem" or "desde segunda até quarta"
- THEN no span SHALL be read, rather than one end of what was asked

### Requirement: Context messages carry the author's platform id

The system SHALL report each message's author in `thread_context` as the
author's platform user id, never as an internal person identifier.

#### Scenario: An author whose row id differs from their Discord id
- WHEN a client reads the context of a message whose author's Discord id is
  not their person row id
- THEN every message SHALL name the Discord id

### Requirement: What a person said is retrieved from their own messages

The system SHALL answer a question about what a named person said with
citations to that person's own messages, retrieved under the same viewer as
any other retrieval, with the author, span, channel permissions and deletions
applied in one statement.

#### Scenario: A multi-author conversation
- WHEN Bea and Caio spoke in the same conversation and someone asks what Bea
  said about it
- THEN the answer SHALL cite Bea's message
- AND Caio's words SHALL NOT reach the reply or the model

#### Scenario: A private channel
- WHEN Lia spoke only in a channel the asker cannot read
- THEN nothing she said there SHALL reach the reply or the model, and the reply
  SHALL be the same as when she said nothing

#### Scenario: A deleted message
- WHEN the person's only matching message was deleted
- THEN it SHALL NOT be cited

### Requirement: A name is resolved only among people the room may know

The system SHALL resolve a mention directly, "eu"/"I" to the asker, and a name
only among people with a visible message in the channels the question's
viewer may read; with several candidates it SHALL ask which, without
retrieval or a model call, and with none it SHALL answer as before.

#### Scenario: Two people with the same first name
- WHEN two people named João have spoken where the asker can read and someone
  asks what João said
- THEN the reply SHALL ask which João, naming both, and no search SHALL run

#### Scenario: A namesake known only from a private channel
- WHEN a third João speaks only in a channel the asker cannot read
- THEN the "which João?" reply SHALL NOT name him

#### Scenario: A name that is not a person
- WHEN someone asks "what did the docs say about X"
- THEN the question SHALL be answered by the ordinary route

#### Scenario: Obligations keep their questions
- WHEN someone asks "o que o João me pediu"
- THEN it SHALL be answered as an obligation question
