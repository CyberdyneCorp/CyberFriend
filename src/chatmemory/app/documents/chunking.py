"""Splitting document prose into retrieval units.

This is *not* `app.windowing`, and the duplication is deliberate. A window is
a run of messages bounded by silence and message count, which are properties
conversation has and a report does not. Applying that rule to a PDF produces
units that begin mid-sentence and end mid-clause, and such a unit embeds to
roughly the average of two unrelated topics -- it retrieves for neither.

So documents split on the boundaries prose actually has: headings first, then
paragraphs, then sentences, and only as a last resort inside a sentence. The
units overlap, because the answer to a question routinely straddles a
paragraph break, and a hard split loses it.

The token *counter* is shared with windowing, because sizing has to agree with
the embedding model's budget either way. The grouping rule is not.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from chatmemory.app.documents.model import DocumentChunk, TextSegment
from chatmemory.app.documents.policy import ChunkingLimits

# Only the estimator is borrowed; see the module docstring.
from chatmemory.app.windowing import TokenCounter, approximate_tokens

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# Markdown ATX headings, and numbered section headings ("3.", "3.1.2 Scope").
_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.{0,80})$")


def detect_headings(lines: Iterable[str], base_location: str) -> list[TextSegment]:
    """Turn plain lines into segments, marking the ones that are headings.

    Used by the text and markdown parsers, which get no structure from the
    format itself. PDF and .docx expose their own and do not come through here.
    """
    segments: list[TextSegment] = []
    for number, raw in enumerate(lines, start=1):
        line = raw.rstrip()
        location = f"{base_location} line {number}" if base_location else f"line {number}"
        atx = _ATX_HEADING.match(line)
        if atx is not None:
            segments.append(TextSegment(atx.group(2), location, heading_level=len(atx.group(1))))
            continue
        numbered = _NUMBERED_HEADING.match(line)
        if numbered is not None and len(line) < 90 and not line.endswith("."):
            level = numbered.group(1).count(".") + 1
            segments.append(TextSegment(line.strip(), location, heading_level=min(level, 6)))
            continue
        segments.append(TextSegment(line, location))
    return segments


def _split_sentences(text: str, count: TokenCounter, budget: int) -> list[str]:
    """Break an over-long paragraph, preferring sentence ends over word gaps."""
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        if count(sentence) <= budget:
            pieces.append(sentence)
            continue
        # A single sentence larger than the budget: minified content, a table
        # rendered as one line, or an attempt to defeat chunking. Split on
        # whitespace so it is still embeddable rather than dropped.
        words = sentence.split()
        current: list[str] = []
        for word in words:
            current.append(word)
            if count(" ".join(current)) >= budget:
                pieces.append(" ".join(current))
                current = []
        if current:
            pieces.append(" ".join(current))
    return [p for p in pieces if p.strip()]


class ProseChunker:
    """Overlapping, heading-aware chunks over a parsed document."""

    def __init__(
        self,
        limits: ChunkingLimits | None = None,
        count_tokens: TokenCounter = approximate_tokens,
    ) -> None:
        self._limits = limits or ChunkingLimits()
        self._count = count_tokens

    def chunk(self, segments: Sequence[TextSegment]) -> list[DocumentChunk]:
        """Split a document, oldest-to-newest in reading order.

        A heading starts a new chunk and is carried into it, so the unit that
        gets embedded says what section it is from -- "Rollback" alone embeds
        very differently from "Rollback" under "Deployment procedure".
        """
        chunks: list[DocumentChunk] = []
        heading: str | None = None
        pending: list[TextSegment] = []
        tokens = 0

        for segment in segments:
            if not segment.text.strip():
                continue
            if segment.is_heading:
                if pending and self._has_body(pending):
                    chunks.append(self._make(len(chunks), pending, heading))
                # A heading is a hard boundary: nothing carries across it,
                # because the text on either side is about a different thing.
                heading = segment.text.strip()
                pending, tokens = [segment], self._count(segment.text)
                continue

            for piece in self._fit(segment):
                size = self._count(piece.text)
                if pending and tokens + size > self._limits.max_tokens:
                    chunks.append(self._make(len(chunks), pending, heading))
                    pending = self._carry_over(pending)
                    tokens = sum(self._count(s.text) for s in pending)
                    if tokens + size > self._limits.max_tokens:
                        # The overlap leaves no room for what follows. A chunk
                        # that repeats its predecessor and adds nothing new is
                        # worse than dropping the overlap here.
                        pending, tokens = [], 0
                pending.append(piece)
                tokens += size

        if pending and self._has_body(pending):
            chunks.append(self._make(len(chunks), pending, heading))
        return chunks

    def _fit(self, segment: TextSegment) -> list[TextSegment]:
        """Break one segment down if it alone exceeds the budget."""
        if self._count(segment.text) <= self._limits.max_tokens:
            return [segment]
        return [
            TextSegment(piece, segment.location)
            for piece in _split_sentences(segment.text, self._count, self._limits.max_tokens)
        ]

    def _carry_over(self, segments: Sequence[TextSegment]) -> list[TextSegment]:
        """The tail of a chunk, repeated at the head of the next one.

        Overlap is measured in tokens and taken whole segments at a time, so
        the repeated text is always readable rather than a truncated fragment.
        """
        if self._limits.overlap_tokens <= 0:
            return []
        carried: list[TextSegment] = []
        total = 0
        for segment in reversed(segments):
            size = self._count(segment.text)
            if total + size > self._limits.overlap_tokens and carried:
                break
            carried.insert(0, segment)
            total += size
        return carried

    def _has_body(self, segments: Sequence[TextSegment]) -> bool:
        """Whether a trailing group is more than a heading with nothing under it."""
        return any(not s.is_heading for s in segments)

    def _make(
        self, ordinal: int, segments: Sequence[TextSegment], heading: str | None
    ) -> DocumentChunk:
        return DocumentChunk(
            ordinal=ordinal,
            text="\n".join(s.text for s in segments).strip(),
            # The first location in the chunk is the one a citation points at:
            # it is where a reader opening the document should start looking.
            location=segments[0].location,
            heading=heading,
        )
