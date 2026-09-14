"""Extraction on ingest: what gets recorded, once, and what never gets asked."""

from __future__ import annotations

from chatmemory.app.asks.candidates import CandidateFilter
from chatmemory.app.asks.extraction import ExtractionService
from chatmemory.app.asks.model import (
    AddresseeKind,
    AskKind,
    AskPolicy,
    ObligationRequest,
    ask_key,
    to_person,
)
from chatmemory.app.asks.resolution import StaticDirectory
from tests.unit.test_asks_support import (
    ALICE,
    BOB,
    BOT,
    OPEN_CHANNEL,
    FakeAskStore,
    StubExtractor,
    extracted,
    message,
    viewer,
)

DIRECTORY = StaticDirectory({"alice": ALICE, "bob": BOB})


def service(
    extractor: StubExtractor,
    store: FakeAskStore,
    bots: frozenset[object] = frozenset(),
) -> ExtractionService:
    return ExtractionService(
        extractor=extractor,
        store=store,
        directory=DIRECTORY,
        candidates=CandidateFilter(bots=frozenset({BOT})) if bots else CandidateFilter(),
    )


async def test_an_ask_is_recorded_against_its_source_message() -> None:
    store = FakeAskStore()
    extractor = StubExtractor(extracted(text="review the migration"))
    source = message(10, ALICE, "@bob can you review the migration?", mentions=frozenset({BOB}))

    report = await service(extractor, store).extract_window([source])

    assert report.candidates == 1
    assert report.recorded == 1
    recorded = next(iter(store.asks.values()))
    assert recorded.source_message_id == 10
    assert recorded.requester == ALICE
    assert recorded.addressee.person == BOB
    assert recorded.kind is AskKind.REQUEST
    assert recorded.asked_at == source.created_at


async def test_the_three_kinds_are_recorded_distinctly() -> None:
    store = FakeAskStore()
    extractor = StubExtractor(
        extracted(kind=AskKind.REQUEST, text="review the migration"),
        extracted(kind=AskKind.QUESTION, text="whether the creds exist"),
        extracted(kind=AskKind.COMMITMENT, text="push the fix tonight"),
    )
    await service(extractor, store).extract_window(
        [message(10, ALICE, "@bob can you review?", mentions=frozenset({BOB}))]
    )
    assert {a.kind for a in store.asks.values()} == set(AskKind)


async def test_a_commitment_is_recorded_against_the_speaker() -> None:
    store = FakeAskStore()
    extractor = StubExtractor(extracted(kind=AskKind.COMMITMENT, text="push the fix"))
    await service(extractor, store).extract_window(
        [message(10, ALICE, "i'll push the fix tonight")]
    )
    assert next(iter(store.asks.values())).addressee.person == ALICE


async def test_reprocessing_the_same_message_does_not_duplicate() -> None:
    """Windows are rebuilt as conversation arrives; re-extraction is normal."""
    store = FakeAskStore()
    extractor = StubExtractor(extracted(text="review the migration"))
    source = message(10, ALICE, "@bob can you review?", mentions=frozenset({BOB}))

    await service(extractor, store).extract_window([source])
    await service(extractor, store).extract_window([source])

    assert len(store.asks) == 1
    assert next(iter(store.asks)) == ask_key(10, AskKind.REQUEST, to_person(BOB))


async def test_two_identical_extractions_in_one_message_collapse() -> None:
    store = FakeAskStore()
    extractor = StubExtractor(extracted(text="review it"), extracted(text="review it again"))
    await service(extractor, store).extract_window(
        [message(10, ALICE, "@bob can you review?", mentions=frozenset({BOB}))]
    )
    assert len(store.asks) == 1


async def test_an_ask_no_longer_extracted_is_withdrawn() -> None:
    store = FakeAskStore()
    source = message(10, ALICE, "@bob can you review?", mentions=frozenset({BOB}))
    await service(StubExtractor(extracted()), store).extract_window([source])
    assert store.asks

    await service(StubExtractor(), store).extract_window([source])
    assert store.asks == {}


async def test_a_sub_threshold_ask_is_recorded_but_never_presented() -> None:
    """Storing it is how the threshold is tuned; presenting it is the harm."""
    store = FakeAskStore()
    await service(StubExtractor(extracted(confidence=0.2)), store).extract_window(
        [message(10, ALICE, "@bob maybe you could look at this?", mentions=frozenset({BOB}))]
    )
    assert len(store.asks) == 1

    policy = AskPolicy()
    presented = await store.obligations(
        viewer(BOB, OPEN_CHANNEL),
        ObligationRequest(min_confidence=policy.min_confidence),
    )
    assert presented == []


async def test_confidence_is_carried_onto_the_record() -> None:
    store = FakeAskStore()
    await service(StubExtractor(extracted(confidence=0.42)), store).extract_window(
        [message(10, ALICE, "@bob can you look?", mentions=frozenset({BOB}))]
    )
    assert next(iter(store.asks.values())).confidence == 0.42


async def test_bot_messages_never_reach_the_extractor() -> None:
    store = FakeAskStore()
    extractor = StubExtractor(extracted())
    await service(extractor, store, bots=frozenset({BOT})).extract_window(
        [message(10, BOT, "@bob can you confirm?", mentions=frozenset({BOB}))]
    )
    assert extractor.calls == []
    assert store.asks == {}


async def test_a_failing_endpoint_does_not_stop_ingestion() -> None:
    store = FakeAskStore()
    report = await service(StubExtractor(fail=True), store).extract_window(
        [message(10, ALICE, "@bob can you review?", mentions=frozenset({BOB}))]
    )
    assert report.failed == 1
    assert report.recorded == 0


async def test_an_unattributed_ask_is_recorded_and_counted_as_such() -> None:
    store = FakeAskStore()
    parent = message(9, BOB, "the new cluster has no terraform")
    report = await service(
        StubExtractor(extracted(addressee_hint="nadia")), store
    ).extract_window(
        [parent, message(10, ALICE, "can nadia take a look at the terraform?", reply_to_id=9)]
    )
    assert report.unattributed == 1
    assert next(iter(store.asks.values())).addressee.kind is AddresseeKind.UNATTRIBUTED
