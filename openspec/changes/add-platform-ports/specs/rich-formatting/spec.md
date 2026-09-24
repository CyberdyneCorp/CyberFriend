## RENAMED Requirements

- FROM: `### Requirement: Answers use Discord markdown`
- TO: `### Requirement: Answers use the platform's markup`

## MODIFIED Requirements

### Requirement: Answers use the platform's markup

Answers SHALL be produced as neutral rich text and rendered in the
destination platform's markup (Discord markdown, Slack `markdown_text` or
mrkdwn, WhatsApp markup) where it aids reading: bold for key figures,
bulleted or numbered lists for several items, code blocks for code, and
sources as subtext lines or the platform's nearest equivalent.

#### Scenario: A price
- WHEN an answer reports a price
- THEN the figure SHALL be emphasised and its source and time SHALL appear as
  subtext, or as a separate plain line where the platform has no subtext

#### Scenario: Code from documentation
- WHEN an answer includes code
- THEN it SHALL be in a fenced or monospace code block

#### Scenario: Discord output unchanged
- WHEN an answer is rendered for Discord
- THEN it SHALL be byte-identical to the output before this change

### Requirement: Long answers split cleanly

An answer longer than the platform renderer's message limit SHALL be split at
a paragraph or list boundary, and SHALL NOT be split inside a code block.

#### Scenario: Long answer with code
- WHEN an answer exceeds the destination platform's limit and contains a code
  block
- THEN each message SHALL contain whole code blocks

### Requirement: Mentions stay suppressed

Formatted answers SHALL NOT notify users, roles, user groups or a whole
channel on any platform: Discord `@everyone`, `@here`, role and user
mentions; Slack `<!here>`, `<!channel>`, `<!everyone>`, `<!subteam^…>` and
`<@U…>`. A person mention SHALL be rendered as a live mention only when the
controller placed it there; a mention produced by the model or copied from
evidence SHALL be rendered as the plain name.

#### Scenario: Mention in answer text
- WHEN answer text contains "@everyone"
- THEN no one SHALL be notified

#### Scenario: A user-group mention in model output
- WHEN model output contains `<!subteam^S0123>`
- THEN no member of that group SHALL be notified

#### Scenario: A model-produced person mention
- WHEN model output contains a person mention in the platform's syntax
- THEN it SHALL be rendered as that person's plain name and nobody SHALL be
  notified
