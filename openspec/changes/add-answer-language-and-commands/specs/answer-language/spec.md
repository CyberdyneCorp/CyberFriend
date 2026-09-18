## Purpose

Write an answer in the language the person asked in, without overriding a
language they chose for themselves.

## ADDED Requirements

### Requirement: An answer is written in the language of the question

The system SHALL write its answer in the language the question was asked in,
when that language is recognised.

#### Scenario: A question in Portuguese
- WHEN someone asks a question in Portuguese
- THEN the answer SHALL be written in Portuguese

#### Scenario: A question in English
- WHEN someone asks a question in English
- THEN the answer SHALL be written in English

#### Scenario: An unrecognised language
- WHEN the language of a question is not recognised
- THEN the answer SHALL still be produced

### Requirement: A saved preferred language wins

A preferred language saved by the person SHALL take precedence over the
language of the question.

#### Scenario: Preferred language differs from the question
- WHEN a person has saved a preferred language
- AND asks a question in another language
- THEN the answer SHALL be in their preferred language

### Requirement: Retrieved content is quoted as written

The system SHALL NOT translate the content it cites.

#### Scenario: Evidence in another language
- WHEN an answer cites a message written in another language
- THEN the quoted excerpt SHALL appear as it was written
