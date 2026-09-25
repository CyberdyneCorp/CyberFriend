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

### Requirement: Suggestions in natural language are confirmed before being stored

When a message explicitly offers a suggestion to the assistant, in Portuguese
or English, the assistant SHALL propose recording it and SHALL store it only
after the person confirms. When the same message is also a question or request
another feature handles, that feature SHALL take it.

#### Scenario: Explicit suggestion
- WHEN a person writes "sugiro que você avise quando alguém me marcar"
- THEN the assistant SHALL offer to record the suggestion
- AND SHALL store it only if the person confirms

#### Scenario: A question phrased as a suggestion
- WHEN a person writes "sugiro que você me diga o preço do BTC"
- THEN the assistant SHALL answer the price question and SHALL NOT propose a
  suggestion

#### Scenario: Declining the proposal
- WHEN the person answers the proposal with "No, answer it"
- THEN nothing SHALL be stored and the message SHALL be handled as it would be
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

When a person asked to be notified, the assistant SHALL send them one direct
message per status change, unless their direct messages are undeliverable.

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
