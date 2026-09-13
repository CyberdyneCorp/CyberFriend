"""Prose chunking, and why it is not conversational windowing.

The properties asserted here are the ones that make a chunk retrievable: it
ends at a boundary a reader would recognise, it carries the heading it sits
under, it overlaps its neighbour, and it knows where in the document it came
from so a citation can point at it.
"""

from __future__ import annotations

from chatmemory.app.documents.chunking import ProseChunker, detect_headings
from chatmemory.app.documents.model import TextSegment
from chatmemory.app.documents.policy import ChunkingLimits

SMALL = ChunkingLimits(max_tokens=40, overlap_tokens=12)


def body(text: str, location: str = "p. 1") -> TextSegment:
    return TextSegment(text, location)


def heading(text: str, location: str = "p. 1", level: int = 1) -> TextSegment:
    return TextSegment(text, location, heading_level=level)


def test_short_documents_stay_in_one_piece() -> None:
    chunks = ProseChunker(SMALL).chunk([body("A single short paragraph.")])
    assert len(chunks) == 1
    assert chunks[0].text == "A single short paragraph."


def test_a_long_document_is_split_into_overlapping_units() -> None:
    paragraphs = [body(f"Paragraph number {i} with a few words in it.") for i in range(12)]
    chunks = ProseChunker(SMALL).chunk(paragraphs)

    assert len(chunks) > 1
    # Overlap: the tail of one chunk reappears at the head of the next, so an
    # answer straddling the boundary is still in one retrievable unit.
    tail = chunks[0].text.splitlines()[-1]
    assert tail in chunks[1].text


def test_headings_are_hard_boundaries_and_are_carried_into_the_chunk() -> None:
    chunks = ProseChunker(SMALL).chunk(
        [
            heading("Deployment"),
            body("Roll forward, never back."),
            heading("Rollback"),
            body("Only with the on-call present."),
        ]
    )
    assert [c.heading for c in chunks] == ["Deployment", "Rollback"]
    # Nothing carries across a heading: the text on either side is about a
    # different thing, and overlapping them blurs both.
    assert "Roll forward" not in chunks[1].text


def test_each_chunk_records_where_in_the_document_it_starts() -> None:
    chunks = ProseChunker(SMALL).chunk(
        [body("Opening line.", "p. 1")] + [body(f"Later line {i}.", "p. 4") for i in range(20)]
    )
    assert chunks[0].location == "p. 1"
    assert any(c.location == "p. 4" for c in chunks)


def test_a_paragraph_larger_than_the_budget_splits_at_sentence_ends() -> None:
    sentences = " ".join(f"This is sentence number {i}." for i in range(40))
    chunks = ProseChunker(SMALL).chunk([body(sentences)])

    assert len(chunks) > 1
    # Split at sentence ends, not mid-clause: a unit that begins mid-sentence
    # embeds as the average of two topics and retrieves for neither.
    assert all(chunk.text.strip().endswith(".") for chunk in chunks)


def test_a_single_unbroken_run_is_still_chunked_rather_than_dropped() -> None:
    """Minified text, or an attempt to defeat chunking, is still indexed."""
    chunks = ProseChunker(SMALL).chunk([body("word " * 600)])
    assert len(chunks) > 1
    assert all(chunk.text for chunk in chunks)


def test_heading_detection_finds_markdown_and_numbered_sections() -> None:
    segments = detect_headings(
        ["# Overview", "Some prose here.", "2.1 Rollback procedure", "More prose."], "p. 2"
    )
    assert [s.text for s in segments if s.is_heading] == ["Overview", "2.1 Rollback procedure"]
    assert segments[0].location == "p. 2 line 1"


def test_a_trailing_heading_with_no_body_is_not_a_chunk() -> None:
    chunks = ProseChunker(SMALL).chunk([body("Body."), heading("Appendix")])
    assert len(chunks) == 1
    assert "Appendix" not in chunks[0].text


def test_chunking_is_not_the_conversational_windower() -> None:
    """A guard against the refactor that "deduplicates" the two.

    Window logic splits on silence gaps and message counts, which a document
    does not have; applying it to prose produces units that break mid-sentence.
    """
    import inspect

    from chatmemory.app import windowing

    chunker_source = inspect.getsource(ProseChunker)
    assert "gap" not in chunker_source
    assert not issubclass(ProseChunker, windowing.WindowBuilder)
