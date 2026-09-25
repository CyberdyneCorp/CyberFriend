## ADDED Requirements

### Requirement: People can suggest features by command

The assistant SHALL record a suggestion given with the suggest command and
confirm it with a reference number, stating that the team will see the text
and the person's name.

#### Scenario: Suggesting with the command
- WHEN a person uses the suggest command with a text
- THEN the assistant SHALL record it and reply with its number
- AND the reply SHALL say that the team will see the text and their name

#### Scenario: The same suggestion twice
- WHEN a person suggests something they already suggested
- THEN no second record SHALL be created
- AND the reply SHALL give the existing number

### Requirement: Suggestions in natural language are explicit and confirmed before being stored

The assistant SHALL recognise a suggestion in a message only when it begins
with one of a fixed set of explicit suggestion forms, in Portuguese ("tenho uma
sugestão", "sugestão:", "seria legal se você") or English ("I have a feature
request", "feature request:", "it would be nice if you could"). It SHALL
propose recording it and SHALL store it only after the person confirms. When
any other feature would handle the message, that feature SHALL take it and no
suggestion SHALL be proposed.

#### Scenario: Explicit suggestion
- WHEN a person writes "tenho uma sugestão: avisar quando alguém me marcar"
- THEN the assistant SHALL offer to record the suggestion
- AND SHALL store it only if the person confirms

#### Scenario: A question phrased as a suggestion
- WHEN a person writes "sugestão: me diga o preço do BTC"
- THEN the assistant SHALL answer the price question and SHALL NOT propose a
  suggestion

#### Scenario: A request another feature handles
- WHEN a person writes "feature request: notify me when BTC hits 100k"
- THEN the alert feature SHALL handle it and no suggestion SHALL be proposed

#### Scenario: The word suggestion in a question
- WHEN a person writes "qual foi a sugestão do João?"
- THEN the assistant SHALL NOT propose a suggestion

#### Scenario: Declining the proposal
- WHEN the person answers the proposal with "No, answer it"
- THEN nothing SHALL be stored and the message SHALL be handled as it would be
  without the suggestion step

#### Scenario: The proposal times out
- WHEN the person does not answer the proposal before it expires
- THEN nothing SHALL be stored and the message SHALL be answered as it would be
  without the suggestion step

#### Scenario: Someone else presses the button
- WHEN a person other than the author presses a proposal button
- THEN it SHALL have no effect

### Requirement: A suggestion stores only the person's own words

A stored suggestion SHALL hold the person's text, language, source kind and
source identifiers, and SHALL NOT hold surrounding messages. A suggestion that
contains an email address, phone number or wallet address SHALL be refused
with the reason.

#### Scenario: Suggestion with an email address
- WHEN a suggestion's text contains an email address
- THEN the assistant SHALL refuse to store it and say why

#### Scenario: Too many suggestions
- WHEN a person has already had 5 suggestions accepted in the last 24 hours
- THEN a further suggestion SHALL be refused and not stored

### Requirement: People can see their own suggestions

The assistant SHALL list a person's own suggestions with their numbers and
statuses, and SHALL NOT list anyone else's.

#### Scenario: Listing
- WHEN a person uses the suggestions command
- THEN they SHALL see only their own suggestions and each one's status

### Requirement: Admins triage suggestions and every change is recorded

The console SHALL list suggestions to operators and admins. Only admins SHALL
change a suggestion's status, note or duplicate link, and every status change
SHALL be recorded with who made it and the values before and after.

#### Scenario: Admin marks a suggestion planned
- WHEN an admin sets a suggestion's status to planned
- THEN the status SHALL change and the change SHALL be recorded

#### Scenario: Operator tries to triage
- WHEN an operator tries to change a suggestion
- THEN the system SHALL refuse it as forbidden

### Requirement: People can be told when their suggestion's status changes

When, and only when, a person asked to be notified, the assistant SHALL send
them one direct message per status change, unless their direct messages are
undeliverable.

#### Scenario: Status changes
- WHEN an admin changes the status of a suggestion whose author asked to be
  notified
- THEN the author SHALL receive one direct message naming the new status

### Requirement: Suggestions follow the person's privacy choices

A person's suggestions SHALL be deleted when they opt out or delete everything,
and no suggestion SHALL be stored for a person who has opted out.

#### Scenario: Opting out
- WHEN a person with suggestions opts out
- THEN their suggestions SHALL be deleted

#### Scenario: Deleting everything
- WHEN a person with suggestions deletes everything, with either choice
- THEN their suggestions SHALL be deleted
