## ADDED Requirements

### Requirement: A connected-app key is taken only in a direct message

The system SHALL store a person's CyberWealth connected-app key only when the
person sends it in a direct message with the assistant, as a message
containing one whole key or through the connect command. It SHALL NOT store a
key sent anywhere else.

#### Scenario: Key sent in a DM
- WHEN a person sends a message containing one well-formed key in a direct message
- THEN the key SHALL be stored for that person
- AND the reply SHALL identify the key by its last four characters only

#### Scenario: Connect command outside a DM
- WHEN the connect command, or any command, carries a key outside a direct message
- THEN nothing SHALL be stored
- AND the reply SHALL be visible only to the person

#### Scenario: Incomplete key
- WHEN a direct message contains `cwk_` but no well-formed key
- THEN nothing SHALL be stored and the reply SHALL say so

#### Scenario: Opted-out person
- WHEN a person who has opted out sends a key
- THEN nothing SHALL be stored

### Requirement: A key never reaches a model, a trace, memory or a reply

A message or command text carrying `cwk_` SHALL NOT be answered as a question,
sent to a model, traced, remembered or stored as a suggestion or a scheduled
question. No reply, listing or log SHALL contain a stored key; only its last
four characters MAY be shown, and only to its owner.

#### Scenario: Key in a DM
- WHEN a person sends a key in a direct message
- THEN no model SHALL be called and no conversation turn SHALL be recorded for it

### Requirement: Keys are encrypted at rest and bound to their owner

The system SHALL store a key only encrypted under a server secret, with the
person and the key kind bound into the encryption, and SHALL keep the key
feature off when the server secret is not configured or unusable.

#### Scenario: Ciphertext moved to another person
- WHEN a stored ciphertext is copied onto another person's row
- THEN it SHALL NOT decrypt, and no key SHALL be used for that person

#### Scenario: Server secret unset
- WHEN the server secret is not configured
- THEN a key sent in a DM SHALL be refused and nothing SHALL be stored

### Requirement: A key is the bearer only on its owner's personal calls in their DM

The system SHALL send a person's key as the bearer only on a personal (`my_`)
tool call to CyberWealth made for an answer delivered to that person's direct
messages, over a connection used for that call alone. Every other call SHALL
NOT carry it.

#### Scenario: Personal call in the owner's DM
- WHEN a run answering in a person's DM calls a personal CyberWealth tool and the person has a key
- THEN the call SHALL carry that person's key and no other credential

#### Scenario: Public call
- WHEN a run calls an `intel_` tool, in a DM or a channel
- THEN the call SHALL NOT carry any person's key

#### Scenario: No key
- WHEN a run in a person's DM calls a personal CyberWealth tool and the person has no key
- THEN nothing SHALL be sent to the server
- AND the invocation SHALL be recorded as refused

#### Scenario: Key refused by CyberWealth
- WHEN CyberWealth answers `401` to a person's key
- THEN the result SHALL be reported as a refused key
- AND the server SHALL remain available for other calls

#### Scenario: Plain-text transport
- WHEN the CyberWealth server is not reached over HTTPS, other than on localhost
- THEN no person's key SHALL be sent to it

### Requirement: Personal tools the service identity cannot list are registered

When keys are on, an allowlisted personal (`my_`) tool on the CyberWealth
server SHALL be registered even if the server's listing for the deployment's
own identity does not include it. Any other allowlisted tool missing from a
reachable server's listing SHALL remain a startup error.

#### Scenario: Unlisted personal tool
- WHEN the service identity's listing omits an allowlisted `my_` tool and keys are on
- THEN the tool SHALL be registered as personal, read-only only if the operator declared it so

### Requirement: Keys are listed and deleted with the person's other data

The privacy command SHALL list a person's connected-app keys by service, last
four characters and date in a DM, and count them in a server channel. Forget
(everywhere, or in a DM), delete everything, opting out and deleting the
person SHALL delete the key.

#### Scenario: Forget everywhere
- WHEN a person forgets everywhere, or forgets in their DM
- THEN their key SHALL be deleted and the reply SHALL say so

#### Scenario: Opt-out or delete everything
- WHEN a person opts out or deletes everything
- THEN their key SHALL be deleted in the same transaction as the rest of their derived data

### Requirement: A key posted in a channel is never archived

A channel message containing `cwk_` SHALL NOT be archived, whether captured
live, edited or backfilled, and SHALL NOT be answered. Its author SHALL be
warned by direct message to revoke the key.

#### Scenario: Key pasted in a channel
- WHEN a person posts a message containing `cwk_` in a server channel
- THEN the message SHALL NOT be archived or answered
- AND the author SHALL receive a direct message telling them to revoke the key and send a new one only in a DM
