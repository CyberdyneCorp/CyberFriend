"""Sniffing and extraction for the formats we accept.

The sniffing tests are the security-relevant half: the format a file is parsed
as must come from its bytes, and must agree with what the uploader claimed.
"""

from __future__ import annotations

from chatmemory.adapters.documents import parsers
from chatmemory.app.documents.model import DocumentFormat, SkipReason
from chatmemory.app.documents.sniffing import classify, declared_format, sniff
from tests.unit.test_documents_corpus import (
    TEST_LIMITS,
    csv_bytes,
    docx,
    markdown,
    pdf,
    zip_of_anything,
)

ALL_FORMATS = frozenset(DocumentFormat)


def check(data: bytes, filename: str, media_type: str | None = None) -> object:
    return classify(data, filename, media_type, ALL_FORMATS, TEST_LIMITS)


# --- sniffing ----------------------------------------------------------


def test_pdf_is_recognised_from_its_magic_bytes() -> None:
    assert sniff(pdf(["hello"]), TEST_LIMITS).fmt is DocumentFormat.PDF


def test_docx_is_recognised_from_the_parts_inside_the_zip() -> None:
    data = docx([("Title", 1), ("Body text", 0)])
    assert sniff(data, TEST_LIMITS).fmt is DocumentFormat.DOCX


def test_a_plain_zip_is_not_a_document() -> None:
    result = sniff(zip_of_anything(), TEST_LIMITS)
    assert result.fmt is None
    assert result.skipped is SkipReason.NOT_ALLOWLISTED


def test_binary_content_is_not_treated_as_text() -> None:
    assert sniff(b"\x00\x01\x02\x03" * 100, TEST_LIMITS).fmt is None


def test_declared_format_prefers_the_extension() -> None:
    assert declared_format("notes.md", "application/octet-stream") is DocumentFormat.MARKDOWN
    assert declared_format("notes", "text/csv") is DocumentFormat.CSV
    assert declared_format("notes.bin", None) is None


def test_text_subtype_comes_from_the_name_because_the_bytes_cannot_say() -> None:
    result = check(b"a,b\n1,2\n", "rows.csv", "text/csv")
    assert result.fmt is DocumentFormat.CSV  # type: ignore[attr-defined]


def test_a_format_off_the_allowlist_is_skipped_unparsed() -> None:
    allowed = frozenset({DocumentFormat.PLAIN_TEXT})
    result = classify(pdf(["hello"]), "report.pdf", None, allowed, TEST_LIMITS)
    assert result.skipped is SkipReason.NOT_ALLOWLISTED


def test_an_oversized_file_is_skipped_before_anything_reads_it() -> None:
    oversized = b"x" * (TEST_LIMITS.max_input_bytes + 1)
    assert check(oversized, "big.txt").skipped is SkipReason.TOO_LARGE  # type: ignore[attr-defined]


# --- extraction --------------------------------------------------------


def test_pdf_text_is_extracted_with_a_page_location_for_each_chunk() -> None:
    outcome = parsers.parse(pdf(["first page", "second page"]), DocumentFormat.PDF, TEST_LIMITS)
    assert outcome.document is not None
    locations = {segment.location for segment in outcome.document.segments}
    assert any(location.startswith("p. 1") for location in locations)
    assert any(location.startswith("p. 2") for location in locations)


def test_pdf_metadata_is_captured_as_content() -> None:
    data = pdf(["body"], {"Title": "Quarterly review", "Keywords": "rollout"})
    outcome = parsers.parse(data, DocumentFormat.PDF, TEST_LIMITS)
    assert outcome.document is not None
    assert ("Title", "Quarterly review") in outcome.document.metadata
    assert outcome.document.title == "Quarterly review"


def test_docx_headings_footnotes_and_properties_all_come_through() -> None:
    data = docx(
        [("Deployment procedure", 1), ("Roll forward, never back.", 0)],
        footnotes=["Approved by the platform team."],
        properties={"title": "Runbook", "creator": "ops"},
    )
    outcome = parsers.parse(data, DocumentFormat.DOCX, TEST_LIMITS)
    assert outcome.document is not None
    headings = [s for s in outcome.document.segments if s.is_heading]
    assert headings and headings[0].text == "Deployment procedure"
    assert any(s.location.startswith("footnote") for s in outcome.document.segments)
    assert dict(outcome.document.metadata)["title"] == "Runbook"


def test_markdown_headings_become_boundaries() -> None:
    data = markdown([("Scope", "What this covers."), ("Risks", "What could go wrong.")])
    outcome = parsers.parse(data, DocumentFormat.MARKDOWN, TEST_LIMITS)
    assert outcome.document is not None
    assert [s.text for s in outcome.document.segments if s.is_heading] == ["Scope", "Risks"]


def test_csv_rows_carry_their_column_names() -> None:
    data = csv_bytes(["service", "owner"], [["api", "ops"], ["web", "frontend"]])
    outcome = parsers.parse(data, DocumentFormat.CSV, TEST_LIMITS)
    assert outcome.document is not None
    body = [s for s in outcome.document.segments if not s.is_heading]
    assert "service: api" in body[0].text


def test_an_empty_file_is_not_indexed() -> None:
    assert parsers.parse(b"   \n  ", DocumentFormat.PLAIN_TEXT, TEST_LIMITS).skipped is (
        SkipReason.EMPTY
    )
