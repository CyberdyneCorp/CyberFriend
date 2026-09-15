"""Source provenance, from the evidence item to the rendered reply.

This tool exists to answer "what did we decide". An answer that blends what a
colleague said with what a search engine returned, and renders both the same
way, is worse than no answer: the reader cannot tell which half is the team's
own record, so they cannot trust either half. `source_system` is carried on
the evidence item; these tests hold it to surviving every hop after that.
"""

from __future__ import annotations

from datetime import UTC, datetime

from chatmemory.adapters.discord.bot import _render
from chatmemory.app.disclosure import ScopedAnswer
from chatmemory.app.reasoning.evidence import (
    SOURCE_DISCORD,
    SOURCE_WEB,
    Evidence,
    EvidenceLedger,
    SourcedCitation,
)
from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.search import RelevanceSource, SearchHit
from chatmemory.ports.answers import Answer, Citation

NOW = datetime(2026, 3, 1, tzinfo=UTC)
GENERAL = ChannelRef("discord", 100)
ELSEWHERE = ChannelRef("web", 0)


def corpus_item(window_id: int) -> Evidence:
    return Evidence(
        window_id=window_id,
        channel=GENERAL,
        text="we decided to hold the freeze until Tuesday",
        score=0.4,
        relevance_source=RelevanceSource.RERANKED,
        url=f"https://discord.com/channels/1/100/{window_id}",
        author_display="sam",
        message_ids=(window_id * 10,),
    )


def web_item(window_id: int) -> Evidence:
    return Evidence(
        window_id=window_id,
        channel=ELSEWHERE,
        text="the vendor's status page reports a rolling outage",
        score=0.4,
        relevance_source=RelevanceSource.RERANKED,
        url=f"https://status.example.com/{window_id}",
        author_display="status.example.com",
        source_system=SOURCE_WEB,
    )


def scoped(*citations: Citation) -> ScopedAnswer:
    return ScopedAnswer(
        answer=Answer("Held until Tuesday; the vendor is still degraded.", citations),
        withheld_from_audience=frozenset(),
    )


# --- the evidence item keeps its kind ----------------------------------


def test_a_citation_carries_the_source_system_of_its_evidence() -> None:
    assert web_item(2).citation().source_system == SOURCE_WEB
    assert corpus_item(1).citation().source_system == SOURCE_DISCORD


def test_only_the_corpus_counts_as_the_corpus() -> None:
    """Stated positively, so a source added later is external by default."""
    assert corpus_item(1).from_corpus
    assert not web_item(2).from_corpus
    assert not web_item(2).citation().from_corpus


def test_a_hit_can_be_tagged_with_the_system_it_came_from() -> None:
    hit = SearchHit(
        window_id=5,
        channel=ELSEWHERE,
        text="text",
        starts_at=NOW,
        ends_at=NOW,
        score=0.1,
        relevance_source=RelevanceSource.FUSED_RRF,
    )
    built = Evidence.from_hit(hit, url="https://example.com", source_system=SOURCE_WEB)

    assert built.source_system == SOURCE_WEB
    assert not built.from_corpus


def test_the_ledger_hands_back_citations_that_still_know_their_source() -> None:
    """The window ids a synthesizer names are resolved here, and this is the
    last hop that holds the evidence: a kind dropped here is unrecoverable."""
    ledger = EvidenceLedger()
    ledger.add([corpus_item(1), web_item(2)])

    citations = ledger.citations([1, 2])

    assert [c.source_system for c in citations] == [SOURCE_DISCORD, SOURCE_WEB]
    assert all(isinstance(c, SourcedCitation) for c in citations)


# --- the reply says which is which -------------------------------------


def test_an_answer_mixing_both_kinds_renders_them_distinguishably() -> None:
    rendered = _render(scoped(corpus_item(1).citation(), web_item(2).citation()))

    assert "**From this server:**" in rendered
    assert "**From the web:**" in rendered
    # The server line comes first, and the web line is tagged inline as well,
    # so a line read on its own still carries the claim about where it came
    # from.
    assert rendered.index("**From this server:**") < rendered.index("**From the web:**")
    assert "(web) [status.example.com](https://status.example.com/2)" in rendered
    assert "(web) [sam]" not in rendered


def test_a_corpus_only_answer_is_labelled_too() -> None:
    """The common case is labelled rather than left to inference: a reader
    should not have to know that the absence of a marker means "ours"."""
    rendered = _render(scoped(corpus_item(1).citation()))

    assert "**From this server:**" in rendered
    assert "**From the web:**" not in rendered


def test_a_plain_port_citation_renders_as_the_corpus() -> None:
    """Everything that predates egress produces a bare `Citation`."""
    rendered = _render(
        scoped(Citation(GENERAL, 1, "kim", "the espresso machine is broken", "https://d/1"))
    )

    assert "**From this server:**" in rendered
    assert "(web)" not in rendered


def test_a_web_citation_without_a_name_does_not_read_as_a_message() -> None:
    anonymous = Evidence(
        window_id=3,
        channel=ELSEWHERE,
        text="text",
        score=0.1,
        relevance_source=RelevanceSource.RERANKED,
        url="https://example.com/3",
        source_system=SOURCE_WEB,
    )
    rendered = _render(scoped(anonymous.citation()))

    assert "open result" in rendered
    assert "jump to message" not in rendered


def test_the_citation_cap_never_hides_a_whole_source() -> None:
    """Five channel hits and one search result must not render as an answer
    that appears to rest on the channel alone."""
    citations = tuple(corpus_item(i).citation() for i in range(1, 7))
    citations += (web_item(99).citation(),)

    rendered = _render(scoped(*citations))

    assert "**From the web:**" in rendered
    assert rendered.count("\n1. ") + rendered.count("\n2. ") <= 2
    # Numbering is continuous across the groups, so the cap is still five.
    assert "6. " not in rendered


def test_an_unknown_source_is_named_rather_than_assumed_to_be_ours() -> None:
    federated = Evidence(
        window_id=4,
        channel=ELSEWHERE,
        text="ticket OPS-12 was closed",
        score=0.1,
        relevance_source=RelevanceSource.RERANKED,
        url="https://tickets.example.com/OPS-12",
        author_display="tickets",
        source_system="ticketing",
    )
    rendered = _render(scoped(federated.citation()))

    assert "**From ticketing:**" in rendered
    assert "(ticketing) [tickets]" in rendered
