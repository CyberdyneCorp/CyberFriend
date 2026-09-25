## ADDED Requirements

### Requirement: Decisions are extracted with asks

The system SHALL read each extraction candidate for the decision it concludes
in the same model call that reads it for asks, and SHALL record a decision
only when the analysed message itself settles a course of action for the
group.

#### Scenario: A conclusion in Portuguese
- WHEN a message such as "fechou, deploy na sexta então" settles a choice
- THEN the system SHALL record a decision attributed to that message and its
  author, dated at the message's time, with a summary in Portuguese

#### Scenario: A proposal
- WHEN a message proposes, asks about or leans towards a choice without
  settling it, such as "bora fazer o deploy na sexta?"
- THEN the system SHALL NOT record a decision

#### Scenario: A status report using a decision word
- WHEN a message reports that something finished, such as "fechou a sprint"
- THEN the system SHALL NOT record a decision

#### Scenario: One array missing from the model's reply
- WHEN the model's reply lacks the decisions array or the asks array
- THEN the system SHALL treat the missing array as empty and keep the other

### Requirement: Decision markers make a message a candidate

The system SHALL treat a message carrying a decision marker, in English or
Brazilian Portuguese, as an extraction candidate even when it mentions no one,
replies to no one and carries no other signal.

#### Scenario: Brazilian phrasing
- WHEN a message says "fechou", "fechado", "bora", "combinado", "ficou
  decidido", "tá decidido", "vamos com" or "vamos seguir com"
- THEN the message SHALL be sent to the extraction model

#### Scenario: English phrasing
- WHEN a message says "we decided", "let's go with", "agreed" or "final call"
- THEN the message SHALL be sent to the extraction model

#### Scenario: No marker and no other signal
- WHEN a message carries neither a decision marker nor an ask signal
- THEN the message SHALL NOT be sent to the extraction model

### Requirement: Decisions are stored stably and withdrawn on re-extraction

The system SHALL identify a decision by its source message and topic, never
by its generated summary, and SHALL replace the decisions of a message each
time that message is extracted.

#### Scenario: Extracted twice
- WHEN the same message is extracted again and the same topic is found
- THEN the system SHALL update the existing decision rather than add another

#### Scenario: An edit takes the decision back
- WHEN a message is edited so that re-extraction finds no decision in it
- THEN the system SHALL remove the decision recorded from it

#### Scenario: An edit removes every signal
- WHEN a message is edited so that it is no longer an extraction candidate
- THEN the system SHALL remove any decision recorded from it

#### Scenario: Storing a decision fails
- WHEN the decision cannot be stored
- THEN the asks extracted from the same message SHALL still be stored

### Requirement: Every message a decision rests on is recorded

The system SHALL record, with each decision, the source message and every
message shown to the model alongside it.

#### Scenario: A decision settling an earlier proposal
- WHEN a decision is extracted from a message whose context includes the
  proposal it settles
- THEN the proposal SHALL be recorded as evidence of that decision

### Requirement: Decisions are embedded when stored

The system SHALL embed each decision's topic and summary when storing it, and
SHALL store the decision without an embedding when embedding fails.

#### Scenario: The embedding endpoint fails
- WHEN the embedding call fails while a decision is stored
- THEN the decision SHALL be stored with no embedding
- AND an embedding stored earlier for it SHALL be kept

### Requirement: Decisions leave with the content they came from

The system SHALL remove a decision when its source message is deleted, when
retention purges it or any message it rests on, and when a person who stated
it or wrote any message it rests on opts out.

#### Scenario: The source message is deleted
- WHEN the source message row is deleted
- THEN its decisions SHALL be deleted with it

#### Scenario: Retention
- WHEN retention runs with a cutoff
- THEN decisions dated before the cutoff SHALL be removed
- AND decisions resting on any message dated before the cutoff SHALL be removed
- AND the retention report SHALL count them

#### Scenario: Opting out of a proposal
- WHEN a person opts out and a decision stated by someone else rests on a
  proposal that person wrote
- THEN that decision SHALL be removed
- AND the opt-out report SHALL count it
