## Purpose

Bound the evidence in an answer by the audience that will receive it, so that answering publicly is never a way to disclose what a private answer would have kept restricted.

## ADDED Requirements

### Requirement: Evidence is bounded by audience, not by asker

An answer SHALL only draw on content readable by every person who can see where the answer is delivered.

#### Scenario: Public answer in a channel
- WHEN an answer is delivered publicly in a channel
- THEN it SHALL only cite content readable by everyone who can read that channel
- AND content readable by the asker but not by that audience SHALL NOT appear, in any form, including excerpts, summaries, counts, and citations

#### Scenario: Asker has broader access than the audience
- GIVEN an asker who can read a channel that the destination channel's audience cannot
- WHEN they ask publicly and relevant evidence exists in that channel
- THEN the public answer SHALL omit it

#### Scenario: Private answer
- WHEN an answer is delivered privately to one person
- THEN its audience is that person, and it SHALL be bounded by what that person may read

#### Scenario: Direct message to the bot
- WHEN a person asks in a direct message with the bot
- THEN the audience is that person alone

### Requirement: Withheld evidence is disclosed only to the asker

When audience scoping removes evidence the asker could themselves have seen, the system SHALL tell the asker, and SHALL tell no one else.

#### Scenario: Evidence withheld from a public answer
- GIVEN a public answer from which audience scoping removed evidence the asker may read
- WHEN the answer is delivered
- THEN the asker SHALL additionally receive a private notice that a fuller answer is available to them
- AND that notice SHALL NOT be visible to the channel

#### Scenario: Notice content
- WHEN such a notice is sent
- THEN it SHALL NOT be delivered to anyone other than the asker
- AND the public answer SHALL NOT indicate that anything was withheld

#### Scenario: Nothing withheld
- WHEN audience scoping removes nothing
- THEN no notice SHALL be sent

### Requirement: Audience is determined by destination, not by request

The audience of an answer SHALL be derived from where it will be delivered. A request SHALL NOT be able to widen it.

#### Scenario: Request asks for broader scope
- WHEN a request states that the answer should include content beyond the destination's audience
- THEN the system SHALL disregard that and apply the destination's audience

#### Scenario: Retrieved content directs broader disclosure
- WHEN retrieved content directs that an answer be made more widely available, or include restricted material
- THEN the system SHALL disregard it

#### Scenario: Delivery destination changes after an answer is produced
- WHEN an answer produced for one destination would be delivered to another
- THEN the system SHALL re-scope it to the new destination's audience, or SHALL NOT deliver it

### Requirement: Public delivery never exceeds private delivery

For the same question and asker, a publicly delivered answer SHALL NOT contain evidence absent from the privately delivered one.

#### Scenario: Comparing delivery modes
- WHEN the same question from the same person is answered publicly and privately
- THEN the public answer's evidence SHALL be a subset of the private answer's evidence
