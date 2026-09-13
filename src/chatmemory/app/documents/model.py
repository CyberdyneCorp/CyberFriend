"""Documents as stored, and the vocabulary the ingestion path speaks.

A document is bytes that entered the corpus through Discord. Everything about
its *visibility* comes from that entry, never from the system the bytes came
from: `DocumentEntry` records which channel let it in, and a document may have
several. That is the whole permission model for documents, deliberately.

`TextSegment` is the parser's output shape rather than a flat string because
the chunker prefers heading and section boundaries, and only the parser knows
where those are: a PDF knows its pages, a .docx knows its heading styles, and
markdown knows its `#`. Flattening to text first throws that away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from chatmemory.domain.identity import ChannelRef, PersonRef


class DocumentOrigin(StrEnum):
    """How a document reached us. Not a permission; permissions come from the channel."""

    ATTACHMENT = "attachment"
    EXTERNAL = "external"


class DocumentFormat(StrEnum):
    """The formats we are willing to parse. A closed set, by design.

    Every entry here is attack surface, so the list is an allowlist that grows
    only deliberately, and the value is what *content sniffing* concluded --
    never what a filename claimed.
    """

    PLAIN_TEXT = "text/plain"
    MARKDOWN = "text/markdown"
    CSV = "text/csv"
    PDF = "application/pdf"
    DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    XML = "application/xml"


class SkipReason(StrEnum):
    """Why a candidate document was not indexed.

    Skipping is the normal outcome for hostile input, so the reasons are a
    closed vocabulary that can be counted and alerted on rather than free text.
    """

    NOT_ALLOWLISTED = "not_allowlisted"
    FORMAT_MISMATCH = "format_mismatch"
    TOO_LARGE = "too_large"
    EXTRACTED_TOO_LARGE = "extracted_too_large"
    TOO_DEEP = "too_deep"
    TIMED_OUT = "timed_out"
    PARSER_FAILED = "parser_failed"
    EMPTY = "empty"
    FETCH_FAILED = "fetch_failed"
    EXTERNAL_DISABLED = "external_disabled"
    SOURCE_NOT_CONFIGURED = "source_not_configured"
    UNRETRIEVABLE = "unretrievable"
    OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True, slots=True)
class TextSegment:
    """One addressable piece of extracted text.

    `location` is what a citation shows a reader -- "p. 4", "footnote 2" --
    so it has to survive chunking, which is why it travels with the text
    rather than being reconstructed afterwards.
    """

    text: str
    location: str
    heading_level: int = 0

    @property
    def is_heading(self) -> bool:
        return self.heading_level > 0


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """What a parser produced from one file.

    `metadata` is carried deliberately, and is treated as content: a PDF's
    /Title and /Keywords are author-controlled text that no reader ever looks
    at, which makes them an attractive place to hide instructions.
    """

    media_type: DocumentFormat
    segments: tuple[TextSegment, ...]
    title: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(s.text for s in self.segments)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """A parse attempt. Either a document or a reason, never neither."""

    document: ExtractedDocument | None = None
    skipped: SkipReason | None = None

    @property
    def ok(self) -> bool:
        return self.document is not None


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    """A retrieval unit of prose.

    Sized and split by `chunking.ProseChunker`, which is not the conversational
    windower: running window logic over a PDF produces units that break
    mid-sentence and retrieve badly.
    """

    ordinal: int
    text: str
    location: str
    heading: str | None = None
    chunk_id: int | None = None


@dataclass(frozen=True, slots=True)
class DocumentEntry:
    """One act of sharing: this document, into this channel, by this message.

    The disclosure decision is the share, so entries are additive and
    independent -- a document that entered through two channels is readable by
    anyone who can read either.
    """

    channel: ChannelRef
    message_id: int
    entered_at: datetime
    uploader: PersonRef | None = None
    attachment_id: int | None = None
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class Document:
    """A parsed document and where it came in.

    `content_hash` is the identity: the same bytes posted in two channels are
    one document with two entries, embedded once.
    """

    content_hash: str
    origin: DocumentOrigin
    media_type: DocumentFormat
    byte_size: int
    title: str | None = None
    external_uri: str | None = None
    source_revision: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    fetched_at: datetime | None = None
    document_id: int | None = None

    @property
    def identity(self) -> str:
        """What makes this document the same document across ingestions.

        For an attachment it is the bytes: the same file posted twice is one
        document with two entries, embedded once. For an external document it
        is the URI, because its content is expected to change underneath us
        and a hash-keyed identity would orphan the entries on every edit.
        """
        return self.external_uri or self.content_hash


@dataclass(frozen=True, slots=True)
class StoredDocument:
    """The result of persisting a document.

    `content_changed` is what drives re-chunking: unchanged bytes must not be
    re-embedded, because one document can cost more than a month of the
    channel's conversation.
    """

    document_id: int
    content_changed: bool


@dataclass(frozen=True, slots=True)
class DocumentHit:
    """A retrieved chunk, with everything a citation needs.

    `entry_message_id` is what links the excerpt back to the moment it was
    shared, which is the only context that explains why the reader is allowed
    to see it.
    """

    chunk_id: int
    document_id: int
    channel: ChannelRef
    text: str
    location: str
    score: float
    title: str | None = None
    heading: str | None = None
    entry_message_id: int | None = None
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class IngestReport:
    """What one ingestion attempt did, for logging and for tests.

    Skips are counted rather than raised: a hostile file is an expected input,
    not an error condition, and ingestion continues past it.
    """

    indexed: int = 0
    skipped: tuple[tuple[str, SkipReason], ...] = ()
    fetch_failures: tuple[str, ...] = ()

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)


@dataclass(frozen=True, slots=True)
class DocumentCost:
    """Embedding cost attributable to documents, kept apart from conversation.

    One upload can produce more chunks than a month of that channel's talk, so
    a combined number hides which of the two is spending the money.
    """

    documents: int = 0
    chunks: int = 0
    estimated_tokens: int = 0
    conversation_windows: int = 0
    conversation_estimated_tokens: int = 0


@dataclass(frozen=True, slots=True)
class FetchRecord:
    """An audit row: what was fetched, from where, and which message linked it.

    Deliberately carries no reason for an unretrievable outcome. "Forbidden"
    and "not found" are different facts about a document's existence, and
    keeping them apart here is how that difference eventually leaks out.
    """

    target: str
    outcome: str
    channel: ChannelRef
    message_id: int
    at: datetime
    document_hash: str | None = None
    attempts: int = 1


@dataclass(frozen=True, slots=True)
class CandidateAttachment:
    """An attachment we may or may not be allowed to look at.

    Carries the *declared* media type and filename so the parser can be told
    what the uploader claimed, and disagree with it.
    """

    attachment_id: int
    filename: str
    declared_media_type: str
    url: str
    byte_size: int
    channel: ChannelRef
    message_id: int
    uploader: PersonRef | None = None
    metadata: dict[str, str] = field(default_factory=dict)
