## Purpose

Turn conversation into a record of who asked what of whom and whether it is still outstanding, so that questions about obligations are answered from state rather than from resemblance.

## ADDED Requirements

### Requirement: Ask identification

The system SHALL identify, in ingested conversation, messages that request something of a person, ask them a question, or commit the speaker to doing something.

#### Scenario: Direct request to a person
- WHEN a message asks a specific person to do something
- THEN the system SHALL record an ask with that message as its source

#### Scenario: Speaker commits to an action
- WHEN a person states that they will do something
- THEN the system SHALL record a commitment attributed to that person

#### Scenario: Statement that asks nothing
- WHEN a message neither requests, asks, nor commits
- THEN the system SHALL NOT record an ask

#### Scenario: Rhetorical or hypothetical phrasing
- WHEN a message is phrased as a question but requests nothing of anyone
- THEN the system SHALL NOT record an ask

### Requirement: Addressee resolution without mention

The system SHALL determine who an ask falls to, including when no one was mentioned.

#### Scenario: Ask in a reply
- WHEN an ask appears in a reply to another person's message
- THEN the system SHALL attribute it to that person unless the ask names someone else

#### Scenario: Ask naming a person without mentioning them
- WHEN an ask refers to a person by name rather than by mention
- THEN the system SHALL attribute it to that person where they can be resolved

#### Scenario: Addressee cannot be determined
- WHEN the intended addressee cannot be determined
- THEN the system SHALL record the ask as unattributed
- AND SHALL NOT attribute it to a person by guessing

#### Scenario: Ask directed at a group
- WHEN an ask is directed at a group rather than an individual
- THEN the system SHALL record it as directed at that group rather than at an arbitrary member

### Requirement: Open and closed state

The system SHALL track whether an ask remains outstanding, using observable events rather than inferred intent.

#### Scenario: Addressee replies after the ask
- WHEN the addressee replies in the same thread after an ask
- THEN the system SHALL treat the ask as answered

#### Scenario: Acknowledgement by reaction
- WHEN the addressee marks the asking message with an acknowledging reaction
- THEN the system SHALL treat the ask as answered

#### Scenario: No response
- WHEN neither has occurred
- THEN the ask SHALL remain open

#### Scenario: Ask ages without response
- WHEN an open ask has been outstanding beyond a configured period
- THEN the system SHALL record it as stale rather than closing it

#### Scenario: Source message deleted
- WHEN the message an ask was extracted from is deleted
- THEN the ask SHALL no longer be reported

### Requirement: Confidence is recorded and acted upon

Each ask SHALL carry a confidence, and the system SHALL NOT present low-confidence extractions as established obligations.

#### Scenario: Low-confidence extraction
- WHEN an ask is extracted with confidence below the configured threshold
- THEN it SHALL NOT be presented as an obligation in a direct answer

#### Scenario: Answer drawn from extracted asks
- WHEN the system answers from extracted asks
- THEN each SHALL be accompanied by a citation resolving to the message it came from, so the reader can check it

### Requirement: Obligation questions are answered from records

Questions about what was asked of a person, or what they owe, SHALL be answered from extracted asks filtered by person and time, not by similarity search.

#### Scenario: What was asked of me today
- WHEN a person asks what was asked of them within a period
- THEN the system SHALL return asks addressed to them whose source falls in that period

#### Scenario: What I need to do
- WHEN a person asks what they need to do
- THEN the system SHALL return their open asks and their own outstanding commitments

#### Scenario: Nothing outstanding
- WHEN a person has no matching asks
- THEN the system SHALL report that there is nothing, as a successful answer

#### Scenario: Asks in channels the requester cannot read
- WHEN asks exist in channels the requesting person may not read
- THEN they SHALL NOT be returned, reported, or counted

### Requirement: Correction

A person SHALL be able to correct the record for an ask concerning them, and corrections SHALL persist.

#### Scenario: Person marks an ask as not applicable
- WHEN the addressee indicates an ask was wrongly extracted or does not fall to them
- THEN the system SHALL stop reporting it as their obligation

#### Scenario: Person marks an ask as done
- WHEN the addressee marks an ask complete
- THEN the system SHALL treat it as closed regardless of other signals

#### Scenario: Re-extraction after correction
- WHEN extraction runs again over a corrected ask's source
- THEN the correction SHALL persist and SHALL NOT be overwritten

#### Scenario: Correction by a different person
- WHEN someone other than the addressee attempts to correct an ask addressed to them
- THEN the system SHALL NOT apply it
