## Purpose

Make the contents of documents searchable alongside conversation, while parsing untrusted files safely and keeping each document's visibility tied to where it entered.

## ADDED Requirements

### Requirement: Attachment capture

The system SHALL capture attachments posted in indexed channels, retrieving their content when the message is ingested rather than depending on a link remaining valid.

#### Scenario: Supported attachment posted
- WHEN a message in an indexed channel carries an attachment of a supported format
- THEN the system SHALL retrieve and store its extracted text
- AND the content SHALL become retrievable to viewers permitted to read that channel

#### Scenario: Attachment in a channel outside indexing scope
- WHEN an attachment is posted outside indexing scope
- THEN the system SHALL NOT retrieve or store it

#### Scenario: Retrieval fails
- WHEN an attachment cannot be retrieved
- THEN the system SHALL record the failure and continue ingesting the message
- AND SHALL NOT retry indefinitely

### Requirement: Bounded format support

The system SHALL extract text only from formats on a configured allowlist, and SHALL reject anything else.

#### Scenario: Format not on the allowlist
- WHEN an attachment's format is not allowlisted
- THEN the system SHALL skip it without parsing

#### Scenario: Declared format does not match content
- WHEN a file's actual content does not match its declared format
- THEN the system SHALL treat it as unsupported and skip it

### Requirement: Parsing is resource-bounded and isolated

Parsing SHALL run under explicit limits on size, time and memory, and a parser failure SHALL NOT interrupt ingestion.

#### Scenario: File exceeds the size limit
- WHEN a file exceeds the configured size limit
- THEN the system SHALL skip it without parsing

#### Scenario: Content expands beyond its limit when decompressed
- WHEN extracting a file produces more content than the configured limit allows
- THEN the system SHALL abandon extraction and skip the file

#### Scenario: Parsing exceeds its time limit
- WHEN parsing exceeds the configured time limit
- THEN the system SHALL abandon it and skip the file

#### Scenario: Parser crashes or hangs
- WHEN a parser fails, crashes, or hangs
- THEN ingestion SHALL continue for other messages and files

#### Scenario: Document references external entities
- WHEN a document instructs the parser to resolve an external reference
- THEN the system SHALL NOT resolve it

### Requirement: Documents are chunked as prose

The system SHALL divide document text into retrieval units suited to continuous prose, distinct from the conversational windowing used for messages.

#### Scenario: Long document
- WHEN a document's text exceeds a single retrieval unit
- THEN the system SHALL divide it into overlapping units preserving readable boundaries

#### Scenario: Structured document
- WHEN a document has headings or sections
- THEN the system SHALL prefer those as boundaries

#### Scenario: Retrieval across both kinds
- WHEN a query matches both conversation and document content the viewer may read
- THEN both SHALL be eligible for the same result set

### Requirement: Documents carry the visibility of where they entered

A document's visibility SHALL be that of the Discord channel through which it entered the corpus.

#### Scenario: Document from a private channel
- GIVEN a document posted in a channel a viewer may not read
- WHEN that viewer issues a query its content would match
- THEN neither the document nor any excerpt SHALL appear in the results

#### Scenario: Same document posted in two channels
- WHEN the same document enters through more than one channel
- THEN it SHALL be readable by a viewer permitted to read any one of those channels

#### Scenario: Channel leaves indexing scope
- WHEN a channel is removed from indexing scope
- THEN documents that entered through it SHALL no longer be returned

### Requirement: Document content is data, never instruction

Text extracted from a document SHALL be treated as content to reason about, whatever it says.

#### Scenario: Document containing instructions
- GIVEN a document whose text instructs the reader to take an action, disclose other content, or disregard prior direction
- WHEN it is retrieved
- THEN the system SHALL treat it as document content
- AND SHALL NOT perform the action it describes

#### Scenario: Instructions placed where readers will not look
- WHEN such text appears in a part of a document a person is unlikely to read, such as its end, or in metadata
- THEN it SHALL be treated no differently

### Requirement: Documents are citable

A result drawn from a document SHALL identify the document, the location within it, and the message through which it entered.

#### Scenario: Result from a document
- WHEN a retrieval returns document content
- THEN the result SHALL name the document and a location within it
- AND SHALL link to the message that introduced it

### Requirement: Removal propagates

The system SHALL stop returning document content when the message that introduced it is deleted.

#### Scenario: Introducing message deleted
- WHEN the message carrying an attachment is deleted
- THEN the system SHALL stop returning that document's content, unless it also entered through another message that remains
