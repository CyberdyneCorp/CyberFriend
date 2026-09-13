## Purpose

Guarantee that a person querying the corpus can only ever receive content from channels they are already permitted to read in Discord, so that indexing conversation never becomes a way to circumvent the server's own permissions.

## ADDED Requirements

### Requirement: Viewer channel visibility resolution

The system SHALL determine, for a given person, the set of indexed channels that person is permitted to read, honouring the server's role permissions and per-channel permission overwrites.

#### Scenario: Member of a channel they can read
- GIVEN a person whose roles grant both the ability to view a channel and to read its history
- WHEN their visible channel set is resolved
- THEN that channel SHALL be included

#### Scenario: Channel the person cannot view
- GIVEN a person whose roles do not grant the ability to view a channel
- WHEN their visible channel set is resolved
- THEN that channel SHALL NOT be included

#### Scenario: Channel viewable without history access
- GIVEN a person who may view a channel but whose permissions deny reading its history
- WHEN their visible channel set is resolved
- THEN that channel SHALL NOT be included

#### Scenario: Person who has left the server
- GIVEN a person who is no longer a member of the server
- WHEN their visible channel set is resolved
- THEN the set SHALL be empty

### Requirement: Mandatory viewer scoping

Every retrieval of message content SHALL be scoped to an identified viewer, and SHALL return only content from that viewer's visible channel set. The system SHALL NOT provide any means of retrieving message content without a viewer.

#### Scenario: Retrieval restricted to visible channels
- GIVEN a corpus containing messages from a channel the viewer cannot read
- WHEN that viewer issues any query, including one whose terms match those messages
- THEN no content from that channel SHALL appear in the result, in any form, including excerpts, summaries, counts, and citations

#### Scenario: Retrieval requested without a viewer
- WHEN a retrieval is requested without an identified viewer
- THEN the system SHALL refuse the request and return an error
- AND SHALL NOT return message content

#### Scenario: Unrecognised viewer
- WHEN a retrieval is requested for a person the system cannot resolve to a server member
- THEN the system SHALL return no message content

### Requirement: Permission changes take effect immediately

The system SHALL reflect a person's current permissions at the time of the query, so that access lost or gained is honoured without reindexing.

#### Scenario: Access revoked
- GIVEN a person previously able to read a channel
- WHEN their access to that channel is removed
- THEN their subsequent queries SHALL return no content from that channel

#### Scenario: Access granted
- GIVEN a person previously unable to read a channel that has already been indexed
- WHEN they are granted access to that channel
- THEN their subsequent queries SHALL be able to return that channel's already-indexed content

### Requirement: Filtering must not silently reduce result quality

The viewer's channel restriction SHALL be applied as part of the search itself rather than to its output, so that restricting a viewer reduces *which* results they see but not *how many* relevant results they receive.

#### Scenario: Restricted viewer requesting a fixed number of results
- GIVEN a corpus in which at least N results relevant to a query exist in channels the viewer may read
- WHEN that viewer requests N results
- THEN the system SHALL return N results

#### Scenario: Approximate search index
- GIVEN the search uses an approximate index that examines only part of the corpus
- WHEN a viewer with access to a small fraction of channels issues a query
- THEN the restriction SHALL constrain what the index examines
- AND the system SHALL NOT return a reduced set produced by discarding inaccessible results after ranking

#### Scenario: Fewer relevant results exist than requested
- GIVEN fewer than N relevant results exist in channels the viewer may read
- WHEN that viewer requests N results
- THEN the system SHALL return those that exist and SHALL NOT report an error
