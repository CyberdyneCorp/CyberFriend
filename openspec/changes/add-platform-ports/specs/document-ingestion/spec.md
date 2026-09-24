## MODIFIED Requirements

### Requirement: Documents carry the visibility of where they entered

A document's visibility SHALL be that of the channel, on its source platform,
through which it entered the corpus; a document uploaded in a one-to-one
conversation with the assistant SHALL instead be visible only to its
uploader.

#### Scenario: Document from a private channel
- GIVEN a document posted in a channel a viewer may not read
- WHEN that viewer issues a query its content would match
- THEN neither the document nor any excerpt SHALL appear in the results

#### Scenario: Same document posted in two channels
- WHEN the same document enters through more than one channel
- THEN it SHALL be readable by a viewer permitted to read any one of those
  channels

#### Scenario: Channel leaves indexing scope
- WHEN a channel is removed from indexing scope
- THEN documents that entered through it SHALL no longer be returned

## ADDED Requirements

### Requirement: A document uploaded in a one-to-one conversation is visible only to its uploader

A document uploaded in a direct message with the assistant, on any platform,
SHALL be stored as owned by the uploading person rather than by a channel,
SHALL be retrievable only by that person (and by identities linked to that
person), and SHALL be deleted when that person forgets everything or opts
out. Each stored document entry SHALL have exactly one of a channel or an
owner.

#### Scenario: Another person never retrieves it
- GIVEN a PDF uploaded by person A in their DM with the assistant
- WHEN person B asks a question its content would match, on any platform
- THEN neither the document nor any excerpt SHALL appear in B's results

#### Scenario: The uploader retrieves it
- WHEN person A asks a question the PDF answers
- THEN its content SHALL be eligible for A's results

#### Scenario: The same file shared in a channel later
- WHEN the uploader later shares the same file in an indexed channel
- THEN it SHALL also be readable by that channel's readers, and removing the
  channel entry SHALL leave the private entry in place
