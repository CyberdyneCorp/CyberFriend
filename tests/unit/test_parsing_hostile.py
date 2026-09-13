"""The hostile-input corpus, run against the parsing path.

Each case asserts two things: the file is skipped, and ingestion survives it.
The second is the one that matters. A parser that rejects an archive bomb but
takes the process down with it has not defended anything, so the last test
here feeds the whole corpus through the real isolated parser, interleaved with
files that are fine, and checks that the good ones still come out.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chatmemory.adapters.documents import parsers
from chatmemory.adapters.documents.isolation import SubprocessParser
from chatmemory.app.documents.model import DocumentFormat, SkipReason
from chatmemory.app.documents.policy import ParsingLimits
from chatmemory.app.documents.sniffing import classify
from tests.unit.test_documents_corpus import (
    TEST_LIMITS,
    billion_laughs,
    deeply_nested_xml,
    docx,
    docx_bomb,
    external_entity_xml,
    malformed_pdf,
    markdown,
    pdf,
    zip_of_anything,
)

ALL_FORMATS = frozenset(DocumentFormat)

pytestmark = pytest.mark.asyncio


def classify_it(data: bytes, filename: str) -> object:
    return classify(data, filename, None, ALL_FORMATS, TEST_LIMITS)


# --- one case per class of hostile input --------------------------------


async def test_archive_bomb_is_refused_before_it_is_extracted() -> None:
    """A docx-shaped zip whose members decompress far past the limit."""
    result = classify_it(docx_bomb(), "quarterly.docx")
    assert result.skipped is SkipReason.EXTRACTED_TOO_LARGE  # type: ignore[attr-defined]


async def test_a_bomb_that_slips_past_sniffing_is_stopped_during_extraction() -> None:
    """Belt and braces: the extractor meters output even if the claim was small.

    The declared sizes in a zip's central directory are attacker-controlled, so
    the size check at classification time is a cheap first pass, never the
    guarantee.
    """
    generous = ParsingLimits(max_extracted_bytes=64 * 1024, max_expansion_ratio=10**9)
    outcome = parsers.parse(docx_bomb(), DocumentFormat.DOCX, generous)
    assert outcome.skipped is SkipReason.EXTRACTED_TOO_LARGE


async def test_malformed_pdf_is_skipped_rather_than_raising() -> None:
    outcome = parsers.parse(malformed_pdf(), DocumentFormat.PDF, TEST_LIMITS)
    assert outcome.document is None
    assert outcome.skipped in (SkipReason.PARSER_FAILED, SkipReason.EMPTY)


async def test_external_entities_are_not_resolved(tmp_path: Path) -> None:
    """The file the document points at must not appear in what we index."""
    secret = tmp_path / "secret.txt"
    secret.write_text("SUPER-SECRET-VALUE")

    outcome = parsers.parse(
        external_entity_xml(str(secret)), DocumentFormat.XML, TEST_LIMITS
    )
    assert outcome.document is None
    rendered = "" if outcome.document is None else outcome.document.text
    assert "SUPER-SECRET-VALUE" not in rendered


async def test_entity_expansion_is_refused() -> None:
    outcome = parsers.parse(billion_laughs(), DocumentFormat.XML, TEST_LIMITS)
    assert outcome.document is None


async def test_deeply_nested_structure_is_abandoned() -> None:
    outcome = parsers.parse(deeply_nested_xml(), DocumentFormat.XML, TEST_LIMITS)
    assert outcome.skipped is SkipReason.TOO_DEEP


async def test_extension_content_mismatch_is_skipped_either_way() -> None:
    """Both directions: a text file claiming to be a PDF, and the reverse."""
    assert classify_it(b"just some notes", "report.pdf").skipped is (  # type: ignore[attr-defined]
        SkipReason.FORMAT_MISMATCH
    )
    assert classify_it(pdf(["hello"]), "notes.txt").skipped is (  # type: ignore[attr-defined]
        SkipReason.FORMAT_MISMATCH
    )


async def test_an_archive_renamed_to_a_document_is_not_parsed() -> None:
    # Not on the allowlist as a zip, and not a PDF as it claims; which of the
    # two reasons wins does not matter, only that nothing parses it.
    result = classify_it(zip_of_anything(), "report.pdf")
    assert result.fmt is None  # type: ignore[attr-defined]
    assert result.skipped in (  # type: ignore[attr-defined]
        SkipReason.NOT_ALLOWLISTED,
        SkipReason.FORMAT_MISMATCH,
    )


# --- isolation: a crash or a hang must not stop ingestion ---------------


async def test_the_isolated_parser_returns_real_text_for_a_good_file() -> None:
    parser = SubprocessParser(TEST_LIMITS)
    outcome = await parser.parse(pdf(["quarterly numbers"]), DocumentFormat.PDF, "q.pdf")
    assert outcome.document is not None
    assert "quarterly" in outcome.document.text


async def test_a_parser_that_hangs_is_killed_rather_than_waited_on() -> None:
    """The limit is enforced from outside the parser, which is the point.

    A parser that has stopped returning cannot enforce its own timeout, so the
    test asserts the *wall clock*: the call comes back at roughly the limit,
    not when the child decides to finish.
    """
    parser = SubprocessParser(ParsingLimits(max_seconds=1.0))
    started = asyncio.get_running_loop().time()
    outcome = await parser.parse(_hanging_xml(), DocumentFormat.XML, "slow.xml")
    elapsed = asyncio.get_running_loop().time() - started

    assert outcome.skipped in (SkipReason.TIMED_OUT, SkipReason.PARSER_FAILED)
    assert elapsed < 10.0


async def test_the_whole_corpus_is_survived_and_good_files_still_index() -> None:
    """Interleave the hostile corpus with valid files, in one pass.

    This is the assertion the change is really about: every bad file is
    skipped, and no bad file prevents the file after it from being indexed.
    """
    parser = SubprocessParser(TEST_LIMITS)
    corpus: list[tuple[str, bytes, DocumentFormat, bool]] = [
        ("bomb.docx", docx_bomb(), DocumentFormat.DOCX, False),
        ("good.pdf", pdf(["rollout plan"]), DocumentFormat.PDF, True),
        ("broken.pdf", malformed_pdf(), DocumentFormat.PDF, False),
        ("notes.md", markdown([("Scope", "one line")]), DocumentFormat.MARKDOWN, True),
        ("xxe.xml", external_entity_xml("/etc/hosts"), DocumentFormat.XML, False),
        ("deep.xml", deeply_nested_xml(), DocumentFormat.XML, False),
        (
            "runbook.docx",
            docx([("Runbook", 1), ("Step one.", 0)]),
            DocumentFormat.DOCX,
            True,
        ),
    ]

    indexed: list[str] = []
    for name, data, fmt, should_index in corpus:
        outcome = await parser.parse(data, fmt, name)
        if outcome.document is not None:
            indexed.append(name)
        assert (outcome.document is not None) is should_index, name

    assert indexed == ["good.pdf", "notes.md", "runbook.docx"]


def _hanging_xml() -> bytes:
    """Large enough that parsing it comfortably outlives a one-second limit."""
    return ('<?xml version="1.0"?><r>' + "<a>x</a>" * 2_000_000 + "</r>").encode()
