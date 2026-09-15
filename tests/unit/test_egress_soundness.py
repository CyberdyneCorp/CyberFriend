"""The egress guard must be unskippable and script-agnostic.

Both properties were asserted by the first implementation and held by neither.
"""

from __future__ import annotations

import pytest

from chatmemory.adapters.web.query import terms
from chatmemory.app.egress import (
    AuthorizedQuery,
    EgressRefused,
    RefusalReason,
    current_authorization,
)

# --- the guard cannot be skipped ---------------------------------------


def test_a_provider_without_clearance_is_refused() -> None:
    """A provider reached by any path that bypassed the guard has no
    clearance to find, so the call cannot proceed."""
    with pytest.raises(EgressRefused) as refused:
        current_authorization("wikipedia")
    assert refused.value.reason is RefusalReason.UNAUTHORIZED


def test_clearance_is_not_transferable_between_providers() -> None:
    """One provider's clearance must not open another's door."""
    from chatmemory.app.egress import (
        EgressGuard,
        EgressRequest,
        ProvenancedQuery,
        authorized,
    )
    from chatmemory.domain.identity import PersonRef

    clearance = EgressGuard().authorize(
        EgressRequest(
            asker=PersonRef("discord", 1),
            query=ProvenancedQuery.from_asker("who wrote Dune"),
            provider="wikipedia",
        )
    )
    with authorized(clearance):
        assert current_authorization("wikipedia").text == "who wrote Dune"
        with pytest.raises(EgressRefused):
            current_authorization("serpapi")


def test_an_authorized_query_cannot_be_constructed_directly() -> None:
    """Otherwise the type is a record any caller can assemble, skipping both
    the provenance check and the audit."""
    from chatmemory.domain.identity import PersonRef

    with pytest.raises(ValueError, match="EgressGuard"):
        AuthorizedQuery(
            text="anything",
            provider="wikipedia",
            asked_by=PersonRef("discord", 1),
            question="anything",
        )


# --- the containment test sees every script ----------------------------


@pytest.mark.parametrize(
    ("script", "text"),
    [
        ("latin", "espresso machine"),
        ("cyrillic", "кофемашина сломана"),
        ("greek", "μηχανή καφέ"),
        ("arabic", "آلة القهوة"),
        ("hebrew", "מכונת קפה"),
        ("cjk", "咖啡机 坏了"),
        ("japanese", "コーヒーメーカー"),
    ],
)
def test_terms_are_found_in_every_script(script: str, text: str) -> None:
    """An ASCII-only word class finds nothing outside Latin, so the
    containment test computes an empty foreign set and passes trivially --
    a query in any other script walked straight through the guard."""
    assert terms(text), f"no terms extracted from {script}"


def test_a_foreign_term_is_detected_in_a_non_latin_script() -> None:
    """The property the ASCII class silently lost."""
    asked = terms("кофемашина сломана")
    sent = terms("кофемашина зарплата")
    assert sent - asked, "a term absent from the question must be visible"


# --- provenance survives the pipeline ----------------------------------


def test_evidence_is_stamped_with_the_result_source() -> None:
    """Provenance is reported per result and stored per item.

    Without carrying it across that boundary every item re-defaults to the
    corpus, and a web result reaches the reader as something a colleague said.
    """
    from datetime import UTC, datetime

    from chatmemory.app.reasoning.evidence import SOURCE_WEB, Evidence, EvidenceLedger
    from chatmemory.app.reasoning.ports import RetrievalResult
    from chatmemory.domain.identity import ChannelRef
    from chatmemory.domain.search import RelevanceSource

    item = Evidence(
        window_id=1,
        channel=ChannelRef("web", 0),
        text="Dune was written by Frank Herbert",
        score=0.9,
        relevance_source=RelevanceSource.VECTOR,
    )
    assert item.source_system == "discord", "default is the conservative one"

    result = RetrievalResult(items=(item,), source_system=SOURCE_WEB)
    ledger = EvidenceLedger()
    ledger.add(result.items, result.source_system)
    stamped = list(ledger.all()) if hasattr(ledger, "all") else list(ledger.items)
    assert all(e.source_system == SOURCE_WEB for e in stamped)
    _ = datetime(2026, 1, 1, tzinfo=UTC)


def test_the_synthesiser_is_told_what_the_source_attribute_means() -> None:
    """The fence emits `source=`, but the prompt never explained it.

    A model that cannot tell corpus from web in its own input will blend them
    in the answer text however carefully the citations are rendered.
    """
    from chatmemory.app.reasoning.stages import SYNTHESIS_SYSTEM

    assert "source=" in SYNTHESIS_SYSTEM
    assert "discord" in SYNTHESIS_SYSTEM


def test_a_web_citation_is_not_judged_by_channel_membership() -> None:
    """It belongs to no channel, so the test refuses it always -- and a
    dropped citation suppresses the whole answer."""
    from chatmemory.app.disclosure import enforce_audience
    from chatmemory.domain.audience import Audience, DeliveryMode
    from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
    from chatmemory.ports.answers import Answer, Citation

    general = ChannelRef("discord", 1)
    asker = PersonRef("discord", 10)
    web = Citation(ChannelRef("web", 0), 0, "Wikipedia", "Frank Herbert",
                   "https://en.wikipedia.org/wiki/Dune", source_system="web")

    scoped = enforce_audience(
        Answer("Frank Herbert.", citations=(web,)),
        Audience(DeliveryMode.PUBLIC_CHANNEL, frozenset({asker}), frozenset({general}), general),
        Viewer(asker, frozenset({general})),
    )
    assert scoped.answer.citations == (web,), "a web citation must survive delivery"
    assert scoped.answer.text == "Frank Herbert."
