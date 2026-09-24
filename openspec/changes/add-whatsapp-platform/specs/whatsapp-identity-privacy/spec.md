## ADDED Requirements

### Requirement: A WhatsApp person is identified by BSUID only

A WhatsApp person SHALL be keyed by their business-scoped user id (BSUID),
and by nothing derived from their phone number. A payload without a BSUID
SHALL NOT be answered and SHALL be counted, masked, for the operator. Two
BSUIDs SHALL NEVER be merged into one person because they share a phone
number. A raw phone number SHALL NOT be used as a person id, display name,
window author, log field, trace attribute, admin-console value or egress
query term.

#### Scenario: A username user without a phone number
- WHEN a message arrives with a BSUID and no phone number
- THEN the person SHALL be served normally
- AND the assistant SHALL ask for a phone number only if a feature needs it

#### Scenario: A payload without a BSUID
- WHEN a message arrives with a phone number and no BSUID
- THEN it SHALL NOT be answered or stored
- AND the operator count of such payloads SHALL increase

#### Scenario: A recycled number with a new BSUID does NOT inherit the old person's data
- WHEN a phone number that belonged to a person with facts, wallets, alerts,
  schedules and a linked Discord identity arrives with a different BSUID
- THEN it SHALL be a new person with no facts, wallets, alerts, schedules or
  link
- AND nothing of the previous person SHALL be shown or delivered to it

#### Scenario: Scanning logs and traces
- WHEN the privacy regression suite scans logs, traces and admin output after
  a WhatsApp conversation
- THEN no phone number and no raw BSUID SHALL appear

### Requirement: The delivery address is kept apart from identity and facts

The address used to send to a WhatsApp person SHALL be stored per person in a
recipient record, BSUID preferred and phone number only when a payload
supplied it, encrypted at rest and never shown, logged, traced or exported.
The phone-as-fact consent flow SHALL neither read nor write the recipient
record.

#### Scenario: A person who declined the phone fact
- WHEN a person declined to save their phone as a fact and an alert fires
- THEN the alert SHALL still be delivered through their recipient record

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

The system SHALL record in the person's notification preference, per
platform, whether they have opted in to proactive messages, defaulting to not
opted in on WhatsApp and leaving Discord and Slack defaults unchanged. `stop`
or `parar` SHALL opt out immediately and cancel held deliveries; `start` or
`começar` SHALL opt in. Replies to the person's own messages SHALL NOT need
opt-in.

#### Scenario: Opting out
- WHEN a person writes "parar"
- THEN no further proactive message SHALL be sent to them
- AND the reply SHALL confirm, in Portuguese, how to opt back in

#### Scenario: Default state
- WHEN a person has never opted in
- THEN no alert, scheduled answer or digest SHALL be sent to them unprompted
- AND the notification outcome SHALL be recorded as `not_opted_in`

### Requirement: Linked team content reaches WhatsApp only when the operator allows it

A WhatsApp identity SHALL be linked to another platform's person only through
the platform-neutral linking flow (`platform-identity`). Team-channel
evidence and ask content from a linked identity SHALL reach WhatsApp only
when `LINKED_CORPUS_ON_WHATSAPP` is true (default false), and the WhatsApp
disclosure SHALL then name Meta as a processor of that content.

#### Scenario: Linked but not allowed
- WHEN a linked person asks on WhatsApp about a team channel and
  `LINKED_CORPUS_ON_WHATSAPP` is false
- THEN no corpus evidence SHALL be used
- AND the reply SHALL say team content is not available on WhatsApp in this
  deployment

#### Scenario: Linked and allowed
- WHEN the setting is true and a linked person asks the same question
- THEN the answer SHALL use their linked viewer, privately, with plain-text
  citations

### Requirement: Forgetting on WhatsApp is explicit and complete

Because WhatsApp sends no usable event when a person deletes a message,
nothing SHALL be deleted implicitly. The person SHALL be able to erase their
conversation memory, individual facts, or everything by command. "Forget
everything" SHALL go through the existing opt-out purge (corpus, conversation
memory and facts, including phone, email, address, birth date and wallets)
and SHALL also delete alerts, schedules, notifications, conversation
summaries, owner-only documents, trace-export rows and traces, then the
WhatsApp identity, recipient, window, held deliveries and inbound rows. For a
person linked to another platform it SHALL remove only the WhatsApp identity,
its link and what was created on WhatsApp, unless the person confirms, a
second time, erasing the whole person. The reply SHALL say that Meta's own
copy is outside the assistant's control.

#### Scenario: Facts, wallets, alerts and schedules are gone after forget everything
- WHEN an unlinked person with a phone fact, two wallets, an alert and a
  schedule writes "forget everything" and confirms
- THEN none of these SHALL remain, nor their memory, documents, traces,
  identity, recipient, window, held deliveries or inbound rows
- AND the reply SHALL mention Meta's retention of up to 30 days

#### Scenario: A linked person's Discord data survives unless they confirm
- WHEN a person linked to Discord writes "forget everything" on WhatsApp and
  confirms once
- THEN the WhatsApp identity and its link SHALL be removed
- AND their Discord facts, wallets, alerts and schedules SHALL remain

#### Scenario: Erasing the whole linked person
- WHEN the same person confirms the second prompt to erase everything
  everywhere
- THEN the full erasure SHALL run for the person on every platform
