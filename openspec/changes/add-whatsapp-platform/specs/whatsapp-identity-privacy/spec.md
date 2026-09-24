## ADDED Requirements

### Requirement: A WhatsApp person is identified without exposing their phone number

A WhatsApp person SHALL be keyed by their business-scoped user id when the
payload carries one, and otherwise by a peppered hash of their phone number.
A raw phone number SHALL NOT be used as a person id, display name, window
author, log field, trace attribute, admin-console value or egress query term.

#### Scenario: A username user without a phone number
- WHEN a message arrives with a BSUID and no phone number
- THEN the person SHALL be served normally
- AND the assistant SHALL ask for a phone number only if a feature needs it

#### Scenario: A legacy payload with only a phone number
- WHEN a message arrives with a phone number and no BSUID
- THEN the person id SHALL be the peppered hash, not the number

#### Scenario: The same person later arrives with a BSUID
- WHEN a person first keyed by phone hash later arrives with both BSUID and
  phone
- THEN both keys SHALL resolve to one person with all their data

#### Scenario: Scanning logs and traces
- WHEN the privacy regression suite scans logs, traces and admin output after
  a WhatsApp conversation
- THEN no phone number and no raw BSUID SHALL appear

### Requirement: The phone number becomes a fact only with consent

The assistant SHALL NOT save a person's WhatsApp phone number as their phone
fact without the person agreeing, and a saved phone fact SHALL follow the
existing DM-only rule on every platform.

#### Scenario: Offering to save the number
- WHEN a person with no saved phone asks the assistant to remember their phone
- THEN the assistant SHALL offer the number it sees, masked, with a confirm
  button
- AND nothing SHALL be saved until they confirm

### Requirement: Proactive messages require recorded opt-in

The system SHALL record, per person per platform, whether they have opted in
to proactive messages on WhatsApp, defaulting to not opted in. `stop` or
`parar` SHALL opt out immediately and cancel held deliveries; `start` or
`começar` SHALL opt in. Replies to the person's own messages SHALL NOT need
opt-in.

#### Scenario: Opting out
- WHEN a person writes "parar"
- THEN no further proactive message SHALL be sent to them
- AND the reply SHALL confirm, in Portuguese, how to opt back in

#### Scenario: Default state
- WHEN a person has never opted in
- THEN no alert, scheduled answer or digest SHALL be sent to them unprompted

### Requirement: A WhatsApp identity is linked to an existing person only by proof

A WhatsApp identity SHALL be linked to an existing Discord or Slack person
only by redeeming a one-time code that the person obtained in a private
conversation on the other platform. Codes SHALL be single use, expire after
ten minutes, be stored hashed, and be limited to five attempts per WhatsApp
identity per hour. Either side SHALL be able to remove the link.

#### Scenario: Linking
- WHEN a Discord person gets code `482913` in their DM and writes
  "link 482913" on WhatsApp within ten minutes
- THEN the identities SHALL be linked
- AND both platforms SHALL confirm the link to that person

#### Scenario: A code requested in a channel
- WHEN a person asks for a link code in a public channel
- THEN no code SHALL be shown there
- AND the reply SHALL point them to a DM

#### Scenario: Guessing codes
- WHEN a WhatsApp identity submits five wrong codes within an hour
- THEN further attempts SHALL be refused for an hour

#### Scenario: Unlinking
- WHEN the person writes "unlink" on either platform
- THEN the WhatsApp identity SHALL lose access to the linked person's corpus
  from its next question

### Requirement: Forgetting on WhatsApp is explicit

Because WhatsApp sends no usable event when a person deletes a message,
nothing SHALL be deleted implicitly; the person SHALL be able to erase their
conversation memory, individual facts, or everything held about their
WhatsApp identity by command, and the reply SHALL say that Meta's own copy is
outside the assistant's control.

#### Scenario: Forget everything
- WHEN a person writes "forget everything" and confirms
- THEN their WhatsApp identity, conversation memory, window, consent, held
  deliveries and dedup records SHALL be deleted
- AND the reply SHALL mention Meta's retention of up to 30 days
