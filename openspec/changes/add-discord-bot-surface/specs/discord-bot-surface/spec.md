## Purpose

Let people reach CyberFriend the way they already talk in Discord — mentioning it, messaging it directly, or running a command — and keep a question answerable as a conversation rather than a single shot.

## ADDED Requirements

### Requirement: Ways to address the bot

The system SHALL answer questions addressed to it by mention, by direct message, and by slash command.

#### Scenario: Mentioned in an indexed channel
- WHEN a person mentions the bot in a channel with a question
- THEN the system SHALL answer in that channel

#### Scenario: Direct message
- WHEN a person sends the bot a direct message
- THEN the system SHALL answer in that direct message

#### Scenario: Slash command
- WHEN a person invokes the bot's question command
- THEN the system SHALL answer in the channel where it was invoked

#### Scenario: Mentioned without a question
- WHEN the bot is mentioned with no answerable request
- THEN the system SHALL respond with what it can do rather than attempting an answer

#### Scenario: Message not addressed to the bot
- WHEN a message in an indexed channel does not address the bot
- THEN the system SHALL NOT reply to it

### Requirement: Asker identity comes from the platform

The person asking SHALL be identified from the platform's own authentication of that message, and SHALL NOT be taken from the message's content.

#### Scenario: Question asked in a channel
- WHEN a person asks a question
- THEN the system SHALL treat the message's authenticated author as the asker

#### Scenario: Message claiming to be from someone else
- WHEN message content states or implies that it comes from a different person
- THEN the system SHALL disregard that and use the authenticated author

### Requirement: Conversational follow-up

The system SHALL keep the context of a conversation so that follow-up questions can refer to what was already said.

#### Scenario: Follow-up in the same thread
- GIVEN the bot has answered in a thread
- WHEN the same person asks a follow-up there
- THEN the system SHALL interpret it in the context of the preceding exchange

#### Scenario: Follow-up by a different person
- WHEN a different person continues a conversation
- THEN the answer SHALL be scoped to that person as the asker, and to the audience of where it is delivered

#### Scenario: Context expiry
- WHEN a conversation has been inactive beyond a configured period
- THEN a further message SHALL be treated as a new question

### Requirement: Responsiveness on long questions

The system SHALL acknowledge a question promptly and indicate that work is in progress, rather than appearing to have failed while a reasoning run completes.

#### Scenario: Question that takes longer than the platform's reply deadline
- WHEN answering will exceed the platform's interaction deadline
- THEN the system SHALL acknowledge within that deadline and deliver the answer when ready

#### Scenario: Answer fails to complete
- WHEN a run ends without an answer
- THEN the system SHALL say so, rather than leaving the acknowledgement unresolved

### Requirement: Per-person limits

The system SHALL limit how much work one person can cause over a period, and SHALL degrade predictably when a limit is reached.

#### Scenario: Person exceeds their rate limit
- WHEN a person asks more questions than their configured limit allows
- THEN the system SHALL decline further questions for that period and say so

#### Scenario: Limit reached mid-conversation
- WHEN a limit is reached
- THEN in-flight answers SHALL complete
- AND the limit SHALL NOT be circumvented by asking through a different surface

### Requirement: Answers carry their sources

An answer SHALL present the evidence it used in a form the reader can follow back to the original message.

#### Scenario: Answer drawn from conversation
- WHEN the system answers from retrieved evidence
- THEN it SHALL include links resolving to the source messages

#### Scenario: Answer with no supporting evidence
- WHEN no evidence supports an answer
- THEN the system SHALL say it found nothing rather than answer unsupported

### Requirement: Bot content is not treated as corpus input

Messages the bot itself produces SHALL NOT become evidence for later answers.

#### Scenario: Bot answer posted in an indexed channel
- WHEN the bot posts an answer in a channel that is indexed
- THEN that answer SHALL NOT be ingested as retrievable evidence
- AND SHALL NOT be citable in a later answer
