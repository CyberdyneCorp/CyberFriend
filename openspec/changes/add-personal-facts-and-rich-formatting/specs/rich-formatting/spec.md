## Purpose

Render answers as readable Discord markdown, while making sure formatting never
lets quoted content disguise a link or break an answer apart.

## ADDED Requirements

### Requirement: Answers use Discord markdown

Answers SHALL use Discord markdown where it aids reading: bold for key figures,
bulleted or numbered lists for several items, code blocks for code, and sources
as subtext lines.

#### Scenario: A price
- WHEN an answer reports a price
- THEN the figure SHALL be emphasised and its source and time SHALL appear as
  subtext

#### Scenario: Code from documentation
- WHEN an answer includes code
- THEN it SHALL be in a fenced code block

### Requirement: Only citation links may be masked

A masked link SHALL appear in an answer only when the assistant generated it for
a citation. Any other link SHALL be shown as its bare address.

#### Scenario: Masked link reproduced from a message
- WHEN answer text contains a masked link that did not come from a citation
- THEN it SHALL be rendered with its destination visible

#### Scenario: Citation link
- WHEN the assistant cites a source
- THEN it MAY render it as a masked link to that source

### Requirement: Quoted content cannot inject formatting

Excerpts quoted from messages SHALL have their markdown neutralised so they
cannot add headings, links, spoilers or code blocks to the answer.

#### Scenario: Excerpt containing a heading
- WHEN an excerpt begins with a markdown heading
- THEN it SHALL be shown as plain text

### Requirement: Long answers split cleanly

An answer longer than Discord's message limit SHALL be split at a paragraph or
list boundary, and SHALL NOT be split inside a code block.

#### Scenario: Long answer with code
- WHEN an answer exceeds the limit and contains a code block
- THEN each message SHALL contain whole code blocks

### Requirement: Mentions stay suppressed

Formatted answers SHALL NOT mention users, roles, @everyone or @here.

#### Scenario: Mention in answer text
- WHEN answer text contains "@everyone"
- THEN no one SHALL be notified
