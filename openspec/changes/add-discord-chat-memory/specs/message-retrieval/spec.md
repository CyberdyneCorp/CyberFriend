## Purpose

Turn the stored corpus into useful answers by grouping fragmentary chat messages into coherent units, searching them by both wording and meaning, and returning results a person can verify against the original conversation.

## ADDED Requirements

### Requirement: Conversational windowing

The system SHALL group related messages into windows and treat the window, rather than the individual message, as the unit of semantic retrieval, because individual chat messages are too short to be matched reliably on meaning alone.

#### Scenario: Consecutive related messages
- WHEN a sequence of messages is posted in a channel within a short span of time
- THEN the system SHALL group them into a common window

#### Scenario: Conversation resumes after a long pause
- GIVEN messages in a channel separated by an extended period of silence
- WHEN windows are formed
- THEN the messages before and after the pause SHALL belong to different windows

#### Scenario: Threaded conversation
- WHEN messages belong to a thread
- THEN the system SHALL group that thread's messages together rather than interleaving them with unrelated channel activity

### Requirement: Hybrid search

The system SHALL rank results using both literal term matching and semantic similarity, so that queries succeed whether or not they reuse the exact wording of the original conversation.

#### Scenario: Query using exact terminology
- GIVEN a conversation containing a distinctive term such as a service name or error code
- WHEN a viewer searches for that term
- THEN the conversation SHALL appear in the results

#### Scenario: Query paraphrasing the conversation
- GIVEN a conversation discussing a topic without using the querent's wording
- WHEN a viewer searches using different words for the same topic
- THEN the conversation SHALL appear in the results

### Requirement: Time-bounded retrieval

The system SHALL support restricting results to an explicit time range, and SHALL apply that restriction as a filter on the stored timestamps rather than inferring recency from the query wording.

#### Scenario: Query restricted to a time range
- WHEN a viewer requests results limited to a given time range
- THEN every returned result SHALL fall within that range
- AND results outside the range SHALL be excluded regardless of how strongly they match the query terms

#### Scenario: Time range containing no matching activity
- WHEN a viewer requests results within a time range that contains no matching messages
- THEN the system SHALL report that there are no results
- AND SHALL NOT substitute results from outside the range

### Requirement: Verifiable citations

Every returned result SHALL identify its source well enough for a person to open the original message in Discord.

#### Scenario: Result returned
- WHEN a retrieval returns a result
- THEN the result SHALL include its channel, author, timestamp, and a link that resolves to the original message

### Requirement: Retracted content excluded

Retrieval SHALL NOT return content that has been deleted at the source or placed outside indexing scope.

#### Scenario: Deleted message matching a query
- GIVEN a message that has been marked deleted
- WHEN a viewer issues a query that its content would have matched
- THEN neither the message nor any excerpt of it SHALL appear in the results

#### Scenario: Window containing a deleted message
- GIVEN a window whose messages include one that has since been deleted
- WHEN that window is returned as a result
- THEN the deleted message's content SHALL be absent from the returned window
