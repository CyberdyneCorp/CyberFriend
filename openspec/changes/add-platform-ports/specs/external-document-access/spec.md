## RENAMED Requirements

- FROM: `### Requirement: Visibility comes from Discord, not the external system`
- TO: `### Requirement: Visibility comes from the source platform, not the external system`

## MODIFIED Requirements

### Requirement: Visibility comes from the source platform, not the external system

An external document's visibility SHALL be that of the channel, on its source
platform, where it was linked, and SHALL NOT be derived from the external
system's own permissions. A link sent in a one-to-one conversation SHALL be
followed only where the platform's feature matrix allows it, and the
resulting document SHALL be visible only to the person who sent it.

#### Scenario: Document linked in a private channel
- GIVEN a document linked in a channel a viewer may not read
- WHEN that viewer issues a query its content would match
- THEN it SHALL NOT appear in the results, even if that viewer could open the
  document directly

#### Scenario: Document linked in two channels
- WHEN a document is linked from more than one channel
- THEN it SHALL be readable by a viewer permitted to read any one of them

#### Scenario: External permissions change
- WHEN a document's permissions change in the external system
- THEN its visibility within the corpus SHALL remain that of the channels it
  was linked in

#### Scenario: A link in a direct message
- WHEN a person sends a configured-source link in their DM on a platform where
  external links are enabled
- THEN the document SHALL be readable only by that person
