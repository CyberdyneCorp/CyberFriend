"""The evidence ledger: deduplication, and what counts as the same query."""

from __future__ import annotations

from datetime import UTC, datetime

from chatmemory.app.reasoning.evidence import Evidence, EvidenceLedger, query_signature
from chatmemory.domain.identity import ChannelRef
from chatmemory.domain.search import RelevanceSource, SearchHit, SearchQuery

NOW = datetime(2026, 3, 1, tzinfo=UTC)
CHANNEL = ChannelRef("discord", 100)


def item(window_id: int) -> Evidence:
    return Evidence(
        window_id=window_id,
        channel=CHANNEL,
        text=f"window {window_id}" * 40,
        score=0.4,
        relevance_source=RelevanceSource.RERANKED,
        url=f"https://discord.com/{window_id}",
        author_display="sam",
        message_ids=(window_id * 10, window_id * 10 + 1),
    )


def test_only_unseen_windows_count_as_new() -> None:
    ledger = EvidenceLedger()
    assert ledger.add([item(1), item(2)]) == 2
    assert ledger.add([item(2), item(3)]) == 1
    assert ledger.window_ids == {1, 2, 3}
    assert len(ledger) == 3


def test_a_query_differing_only_in_word_order_is_the_same_query() -> None:
    ledger = EvidenceLedger()
    assert ledger.note_query(SearchQuery(text="deploy script owner"))
    assert not ledger.note_query(SearchQuery(text="Owner DEPLOY script"))


def test_a_wider_result_count_or_time_range_is_a_different_query() -> None:
    ledger = EvidenceLedger()
    assert ledger.note_query(SearchQuery(text="deploys", limit=20))
    assert ledger.note_query(SearchQuery(text="deploys", limit=40))
    assert ledger.note_query(SearchQuery(text="deploys", limit=40, since=NOW))


def test_the_signature_covers_the_facets_a_correction_may_change() -> None:
    base = SearchQuery(text="deploys")
    assert query_signature(base) != query_signature(SearchQuery(text="deploys", until=NOW))


def test_citations_resolve_only_ids_the_run_holds() -> None:
    ledger = EvidenceLedger()
    ledger.add([item(1), item(2)])
    citations = ledger.citations([2, 999, 2, 1])

    assert [c.message_id for c in citations] == [20, 10]
    assert all(c.author_display == "sam" for c in citations)
    assert len(citations[0].excerpt) <= 240


def test_evidence_is_built_from_a_search_hit_with_its_link() -> None:
    hit = SearchHit(
        window_id=5,
        channel=CHANNEL,
        text="text",
        starts_at=NOW,
        ends_at=NOW,
        score=0.016,
        relevance_source=RelevanceSource.FUSED_RRF,
        message_ids=(50,),
    )
    built = Evidence.from_hit(hit, url="https://discord.com/5", author_display="kim")

    assert built.window_id == 5
    assert built.relevance_source is RelevanceSource.FUSED_RRF
    assert built.citation().url == "https://discord.com/5"
    assert built.source_system == "discord"


def test_a_window_with_no_resolvable_message_still_cites_its_channel() -> None:
    orphan = Evidence(
        window_id=9,
        channel=CHANNEL,
        text="text",
        score=0.1,
        relevance_source=RelevanceSource.RERANKED,
    )
    assert orphan.citation().message_id == 0
    assert orphan.citation().channel == CHANNEL
