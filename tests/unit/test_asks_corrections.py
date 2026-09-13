"""Corrections: whose word counts, and that it keeps counting."""

from __future__ import annotations

from chatmemory.app.asks.candidates import CandidateFilter
from chatmemory.app.asks.corrections import CorrectionService, may_correct
from chatmemory.app.asks.extraction import ExtractionService
from chatmemory.app.asks.model import (
    UNATTRIBUTED,
    Correction,
    CorrectionOutcome,
    CorrectionResolution,
    ObligationRequest,
    to_group,
)
from chatmemory.app.asks.resolution import StaticDirectory
from tests.unit.test_asks_support import (
    ALICE,
    BOB,
    CARA,
    OPEN_CHANNEL,
    PRIVATE_CHANNEL,
    FakeAskStore,
    StubExtractor,
    ask,
    extracted,
    message,
    viewer,
)

DIRECTORY = StaticDirectory({"alice": ALICE, "bob": BOB})


def seeded() -> tuple[FakeAskStore, str]:
    store = FakeAskStore()
    item = ask(source_message_id=10, requester=ALICE)
    store.asks[item.key] = item
    return store, item.key


async def test_the_addressee_can_mark_an_ask_done() -> None:
    store, key = seeded()
    outcome = await CorrectionService(store).mark_done(viewer(BOB, OPEN_CHANNEL), key)
    assert outcome is CorrectionOutcome.APPLIED

    remaining = await store.obligations(viewer(BOB, OPEN_CHANNEL), ObligationRequest())
    assert remaining == []


async def test_the_addressee_can_mark_an_ask_not_applicable() -> None:
    store, key = seeded()
    outcome = await CorrectionService(store).mark_not_applicable(
        viewer(BOB, OPEN_CHANNEL), key
    )
    assert outcome is CorrectionOutcome.APPLIED
    assert await store.count_outstanding(viewer(BOB, OPEN_CHANNEL), ObligationRequest()) == 0


async def test_somebody_else_cannot_correct_your_asks() -> None:
    """Otherwise closing another person's obligations is a way to hide them."""
    store, key = seeded()
    outcome = await CorrectionService(store).mark_done(viewer(CARA, OPEN_CHANNEL), key)
    assert outcome is CorrectionOutcome.NOT_ADDRESSEE
    assert await store.count_outstanding(viewer(BOB, OPEN_CHANNEL), ObligationRequest()) == 1


async def test_the_requester_cannot_close_an_ask_they_made() -> None:
    store, key = seeded()
    outcome = await CorrectionService(store).mark_done(viewer(ALICE, OPEN_CHANNEL), key)
    assert outcome is CorrectionOutcome.NOT_ADDRESSEE


async def test_correcting_an_ask_in_an_unreadable_channel_reports_unknown() -> None:
    """The difference between "not yours" and "not here" is itself a disclosure."""
    store = FakeAskStore()
    item = ask(source_message_id=10, channel=PRIVATE_CHANNEL)
    store.asks[item.key] = item
    outcome = await CorrectionService(store).mark_done(viewer(BOB, OPEN_CHANNEL), item.key)
    assert outcome is CorrectionOutcome.UNKNOWN_ASK


async def test_an_unknown_ask_reports_unknown() -> None:
    store, _ = seeded()
    outcome = await CorrectionService(store).mark_done(viewer(BOB, OPEN_CHANNEL), "nope")
    assert outcome is CorrectionOutcome.UNKNOWN_ASK


async def test_a_dismissed_ask_does_not_come_back_when_its_window_is_reprocessed() -> None:
    """Without this the system reads as ignoring the person who corrected it."""
    store = FakeAskStore()
    source = message(10, ALICE, "@bob can you review the migration?", mentions=frozenset({BOB}))
    service = ExtractionService(
        extractor=StubExtractor(extracted()),
        store=store,
        directory=DIRECTORY,
        candidates=CandidateFilter(),
    )
    await service.extract_window([source])
    key = next(iter(store.asks))
    await CorrectionService(store).mark_not_applicable(viewer(BOB, OPEN_CHANNEL), key)

    await service.extract_window([source])

    assert key in store.corrections
    assert await store.obligations(viewer(BOB, OPEN_CHANNEL), ObligationRequest()) == []


async def test_a_group_ask_is_nobody_single_person_to_close() -> None:
    """One member clearing a group obligation hides it from the rest."""
    item = ask(addressee=to_group("the platform team"))
    assert not may_correct(item, BOB)


async def test_an_unattributed_ask_cannot_be_corrected() -> None:
    assert not may_correct(ask(addressee=UNATTRIBUTED), BOB)


async def test_a_correction_assembled_for_somebody_else_is_refused() -> None:
    """Defence in depth: the service builds it from the viewer, but the store checks."""
    store, key = seeded()
    outcome = await store.apply_correction(
        viewer(BOB, OPEN_CHANNEL),
        Correction(ask_key=key, by=CARA, resolution=CorrectionResolution.DONE),
    )
    assert outcome is CorrectionOutcome.NOT_ADDRESSEE
