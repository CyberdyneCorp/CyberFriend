"""Text extraction from the allowlisted formats.

Everything in this module runs against bytes an arbitrary server member
uploaded, and is expected to be attacked. The rules it follows:

  * nothing is trusted from the container -- declared sizes, entity
    declarations and nesting depth are all attacker-controlled;
  * extraction is *metered*, so a file that decompresses forever is abandoned
    partway rather than filling memory;
  * XML is parsed with defusedxml, with DTDs, entities and external references
    all refused, in every format that is XML underneath -- which includes
    .docx, since a .docx is a zip full of XML;
  * a failure is a skip. Nothing here raises past `parse()`.

The wall-clock and memory limits are *not* here: a parser that hangs cannot
enforce its own timeout. Those live in `isolation.py`, which kills the process.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from typing import cast
from xml.etree.ElementTree import Element

from charset_normalizer import from_bytes
from defusedxml import ElementTree as DefusedET  # type: ignore[import-untyped]

from chatmemory.app.documents.chunking import detect_headings
from chatmemory.app.documents.model import (
    DocumentFormat,
    ExtractedDocument,
    ParseOutcome,
    SkipReason,
    TextSegment,
)
from chatmemory.app.documents.policy import ParsingLimits

# Word stores footnotes and endnotes outside document.xml, and no reader ever
# scrolls to them -- which is exactly why they are indexed, and exactly why
# they are fenced as content like everything else.
DOCX_BODY = "word/document.xml"
DOCX_NOTES = ("word/footnotes.xml", "word/endnotes.xml")
DOCX_CORE_PROPERTIES = "docProps/core.xml"

W_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

MAX_CSV_FIELD = 131072


class ExtractionTooLarge(Exception):
    """Raised when metered extraction passes its budget. Caught in `parse`."""


class TooDeep(Exception):
    """Raised when a structure nests deeper than we are willing to walk."""


class Budget:
    """A running total of extracted characters, checked as text is produced.

    Declared sizes are a hint; this is the enforcement. A zip that announces
    four kilobytes and produces four gigabytes is stopped here, on the way out,
    rather than after the fact.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max = max_bytes
        self._used = 0

    def take(self, text: str) -> str:
        self._used += len(text.encode("utf-8", "ignore"))
        if self._used > self._max:
            raise ExtractionTooLarge
        return text


def decode(data: bytes) -> str:
    """Best-effort decode of an unknown text encoding.

    charset-normalizer rather than chardet: it is a dependency we already
    carry, and a wrong guess here costs mojibake, never a crash.
    """
    best = from_bytes(data).best()
    if best is not None:
        return str(best)
    return data.decode("utf-8", "replace")


def _plain_text(data: bytes, budget: Budget, base: str = "") -> list[TextSegment]:
    text = budget.take(decode(data))
    return detect_headings(text.splitlines(), base)


def _csv_segments(data: bytes, budget: Budget) -> list[TextSegment]:
    """Rows as segments, with the header line carried into each one.

    A bare row of values embeds badly -- "42, 17, closed" means nothing on its
    own -- so each row is rendered with its column names.
    """
    text = decode(data)
    csv.field_size_limit(MAX_CSV_FIELD)
    reader = csv.reader(io.StringIO(text))
    segments: list[TextSegment] = []
    header: list[str] = []
    for number, row in enumerate(reader, start=1):
        if not row:
            continue
        if not header:
            header = [c.strip() for c in row]
            segments.append(TextSegment(budget.take(", ".join(header)), "row 1", heading_level=1))
            continue
        rendered = "; ".join(
            f"{header[i] if i < len(header) else f'column {i + 1}'}: {value}"
            for i, value in enumerate(row)
        )
        segments.append(TextSegment(budget.take(rendered), f"row {number}"))
    return segments


def _pdf_segments(data: bytes, budget: Budget) -> tuple[list[TextSegment], list[tuple[str, str]]]:
    # Imported lazily: pypdf is only needed when a PDF actually arrives, and
    # importing it is not free.
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    segments: list[TextSegment] = []
    for number, page in enumerate(reader.pages, start=1):
        text = budget.take(page.extract_text() or "")
        segments.extend(detect_headings(text.splitlines(), f"p. {number}"))

    metadata: list[tuple[str, str]] = []
    raw: dict[str, object] = dict(reader.metadata or {})
    for key, value in raw.items():
        if isinstance(value, str) and value.strip():
            metadata.append((str(key).lstrip("/"), budget.take(value)))
    return segments, metadata


def _xml_root(data: bytes) -> Element:
    """Parse XML with every external-resolution feature refused.

    forbid_dtd stops the entity declarations a billion-laughs or
    external-entity document needs, and forbid_external stops SYSTEM
    references reaching the network or the filesystem. Both matter: an
    uploaded .xml or .docx is an attacker's document, and the parser is the
    thing being addressed.
    """
    root = DefusedET.fromstring(
        data, forbid_dtd=True, forbid_entities=True, forbid_external=True
    )
    # defusedxml ships no type information; the cast is the boundary where
    # that Any becomes a real type rather than spreading through the module.
    return cast(Element, root)


def _walk(root: Element, limits: ParsingLimits, budget: Budget) -> Iterator[TextSegment]:
    """Depth-bounded, iterative walk. Recursion here is a stack overflow.

    A document nested ten thousand levels deep is a small file, and walking it
    recursively is how a small file takes the process down.
    """
    stack: list[tuple[Element, int, str]] = [(root, 0, "")]
    while stack:
        element, depth, path = stack.pop()
        if depth > limits.max_nesting_depth:
            raise TooDeep
        here = f"{path}/{element.tag}" if path else str(element.tag)
        text = (element.text or "").strip()
        if text:
            yield TextSegment(budget.take(text), here)
        tail = (element.tail or "").strip()
        if tail:
            yield TextSegment(budget.take(tail), here)
        stack.extend((child, depth + 1, here) for child in reversed(list(element)))


def _local_name(tag: str) -> str:
    """Drop the XML namespace: `{...dc/1.1/}title` is just `title` to a reader."""
    return tag.rsplit("}", 1)[-1]


def _docx_text(element: Element) -> str:
    return "".join(t.text or "" for t in element.iter(f"{W_NAMESPACE}t")).strip()


def _docx_heading_level(element: Element) -> int:
    style = element.find(f"{W_NAMESPACE}pPr/{W_NAMESPACE}pStyle")
    if style is None:
        return 0
    name = (style.get(f"{W_NAMESPACE}val") or "").lower()
    if not name.startswith("heading"):
        return 0
    digits = "".join(c for c in name if c.isdigit())
    return min(int(digits), 6) if digits else 1


def _docx_part(archive: zipfile.ZipFile, name: str, limits: ParsingLimits) -> bytes | None:
    """Read one zip member, refusing one that expands past its declared size.

    `ZipFile.read` will happily produce gigabytes from a small member, so the
    read is capped and a member that exceeds its cap is treated as a bomb.
    """
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limits.max_extracted_bytes:
        raise ExtractionTooLarge
    with archive.open(info) as handle:
        data = handle.read(limits.max_extracted_bytes + 1)
    if len(data) > limits.max_extracted_bytes:
        raise ExtractionTooLarge
    return data


def _docx_segments(
    data: bytes, limits: ParsingLimits, budget: Budget
) -> tuple[list[TextSegment], list[tuple[str, str]]]:
    archive = zipfile.ZipFile(io.BytesIO(data))
    body = _docx_part(archive, DOCX_BODY, limits)
    if body is None:
        raise ValueError("not a word document")

    segments: list[TextSegment] = []
    root = _xml_root(body)
    for number, paragraph in enumerate(root.iter(f"{W_NAMESPACE}p"), start=1):
        text = budget.take(_docx_text(paragraph))
        if text:
            segments.append(
                TextSegment(text, f"paragraph {number}", _docx_heading_level(paragraph))
            )

    for part in DOCX_NOTES:
        note = _docx_part(archive, part, limits)
        if note is None:
            continue
        label = "footnote" if "footnotes" in part else "endnote"
        note_root = _xml_root(note)
        for number, paragraph in enumerate(note_root.iter(f"{W_NAMESPACE}p"), start=1):
            text = budget.take(_docx_text(paragraph))
            if text:
                segments.append(TextSegment(text, f"{label} {number}"))

    metadata: list[tuple[str, str]] = []
    core = _docx_part(archive, DOCX_CORE_PROPERTIES, limits)
    if core is not None:
        for element in _xml_root(core):
            value = (element.text or "").strip()
            if value:
                metadata.append((_local_name(element.tag), budget.take(value)))
    return segments, metadata


def _title_from(segments: list[TextSegment], metadata: list[tuple[str, str]]) -> str | None:
    for key, value in metadata:
        if key.lower().endswith("title") and value.strip():
            return value.strip()
    heading = next((s for s in segments if s.is_heading), None)
    return heading.text if heading is not None else None


def parse(data: bytes, fmt: DocumentFormat, limits: ParsingLimits) -> ParseOutcome:
    """Extract text, or say why not. Never raises for hostile input.

    The broad except is the point of the function: the caller is an ingestion
    loop, and one unparseable upload must cost that upload and nothing else.
    """
    budget = Budget(limits.max_extracted_bytes)
    try:
        segments, metadata = _dispatch(data, fmt, limits, budget)
    except ExtractionTooLarge:
        return ParseOutcome(skipped=SkipReason.EXTRACTED_TOO_LARGE)
    except TooDeep:
        return ParseOutcome(skipped=SkipReason.TOO_DEEP)
    except RecursionError:
        return ParseOutcome(skipped=SkipReason.TOO_DEEP)
    except Exception:  # noqa: BLE001 - malformed input is expected, not exceptional
        return ParseOutcome(skipped=SkipReason.PARSER_FAILED)

    document = ExtractedDocument(
        media_type=fmt,
        segments=tuple(segments),
        title=_title_from(segments, metadata),
        metadata=tuple(metadata),
    )
    if document.is_empty:
        return ParseOutcome(skipped=SkipReason.EMPTY)
    return ParseOutcome(document=document)


def _dispatch(
    data: bytes, fmt: DocumentFormat, limits: ParsingLimits, budget: Budget
) -> tuple[list[TextSegment], list[tuple[str, str]]]:
    if fmt is DocumentFormat.PDF:
        return _pdf_segments(data, budget)
    if fmt is DocumentFormat.DOCX:
        return _docx_segments(data, limits, budget)
    if fmt is DocumentFormat.XML:
        return list(_walk(_xml_root(data), limits, budget)), []
    if fmt is DocumentFormat.CSV:
        return _csv_segments(data, budget), []
    return _plain_text(data, budget), []
