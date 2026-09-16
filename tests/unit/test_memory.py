"""What conversation memory will write, and on what provenance.

The store tests (tests/integration/test_memory_store.py) show that a turn is
withheld when its channels are unreadable. That check is only as good as the
channels recorded, so these pin down how an answer's provenance is worked out
-- and that anything whose readability cannot be re-checked is not stored.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from chatmemory.app.memory import ConversationMemory, answer_provenance
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.answers import Answer, Citation
from chatmemory.ports.memory import (
    ConversationLocation,
    MemoryPurge,
    MemoryStore,
    Recollection,
    RememberedSummary,
    RememberedTurn,
)

ALICE = PersonRef("discord", 1)
GENERAL = ChannelRef("discord", 10)
ENG = ChannelRef("discord", 11)
HERE = ConversationLocation("discord", 10, direct=False)
WEB = ChannelRef("web", 0)
NO_MEMORY = Recollection()


def cite(channel: ChannelRef, source_system: str = "discord") -> Citation:
    return Citation(channel, 1, "someone", "excerpt", "https://x", source_system=source_system)


class RecordingStore:
    def __init__(self) -> None:
        self.turns: list[
            tuple[PersonRef, ConversationLocation, str, str, frozenset[ChannelRef]]
        ] = []
        self.recalls: list[tuple[Viewer, ConversationLocation, int]] = []

    async def record_turn(
        self,
        person: PersonRef,
        location: ConversationLocation,
        question: str,
        answer: str,
        source_channels: frozenset[ChannelRef],
    ) -> bool:
        self.turns.append((person, location, question, answer, source_channels))
        return True

    async def recall(
        self, viewer: Viewer, location: ConversationLocation, turn_limit: int
    ) -> Recollection:
        self.recalls.append((viewer, location, turn_limit))
        return Recollection()

    async def record_summary(
        self, person: PersonRef, location: ConversationLocation, text: str, through_turn_id: int
    ) -> bool:
        return True

    async def forget(
        self, person: PersonRef, location: ConversationLocation | None
    ) -> MemoryPurge:
        return MemoryPurge()

    async def purge_before(self, cutoff: datetime) -> MemoryPurge:
        return MemoryPurge()


def _conforms(store: RecordingStore) -> MemoryStore:
    return store


# --- provenance ---------------------------------------------------------


def test_corpus_citations_are_recorded_by_channel() -> None:
    answer = Answer("a", citations=(cite(GENERAL), cite(ENG)))
    assert answer_provenance(HERE, answer, (), NO_MEMORY) == {GENERAL, ENG}


def test_consulted_but_uncited_channels_are_part_of_provenance() -> None:
    """An uncited sentence paraphrased from a window must not outlive a
    revocation of that window's channel."""
    answer = Answer("a", citations=(cite(GENERAL),))
    assert answer_provenance(HERE, answer, (ENG,), NO_MEMORY) == {GENERAL, ENG}


def test_web_results_carry_no_channel() -> None:
    answer = Answer("a", citations=(cite(WEB, source_system="web"),))
    assert answer_provenance(HERE, answer, (WEB,), NO_MEMORY) == frozenset()


def test_an_answer_resting_on_nothing_has_empty_not_unknown_provenance() -> None:
    assert answer_provenance(HERE, Answer("hello"), (), NO_MEMORY) == frozenset()


def test_an_unknown_source_system_makes_provenance_unknown() -> None:
    """A source added later, or a federated tool with its own access rules,
    cannot be re-checked against Discord channels -- so fail closed."""
    answer = Answer("a", citations=(cite(GENERAL), cite(ChannelRef("issues", 0), "issues")))
    assert answer_provenance(HERE, answer, (), NO_MEMORY) is None


def test_an_unknown_consulted_platform_makes_provenance_unknown() -> None:
    answer = Answer("a", citations=(cite(GENERAL),))
    assert answer_provenance(HERE, answer, (ChannelRef("issues", 0),), NO_MEMORY) is None


def _shown(*channels: ChannelRef, summary: frozenset[ChannelRef] = frozenset()) -> Recollection:
    when = datetime(2026, 9, 1)
    return Recollection(
        summaries=(RememberedSummary("digest", 1, when, summary),) if summary else (),
        turns=(RememberedTurn(2, "q", "a", when, frozenset(channels)),),
    )


def test_a_turn_inherits_the_channels_of_the_memory_it_was_shown() -> None:
    """Regression: a follow-up may restate what it was reminded of."""
    answer = Answer("about that: general mentions lunch", citations=(cite(GENERAL),))
    shown = _shown(ENG, summary=frozenset({ChannelRef("discord", 12)}))
    assert answer_provenance(HERE, answer, (), shown) == {
        GENERAL,
        ENG,
        ChannelRef("discord", 12),
    }


def test_a_web_only_turn_shown_memory_is_not_channel_free() -> None:
    """The empty set passes every check; a web answer shown #eng memory is not empty."""
    assert answer_provenance(HERE, Answer("per the web"), (WEB,), _shown(ENG)) == {ENG}


def test_memory_on_a_foreign_platform_makes_provenance_unknown() -> None:
    shown = _shown(ChannelRef("slack", 11))
    assert answer_provenance(HERE, Answer("a", citations=(cite(GENERAL),)), (), shown) is None


# --- the service ---------------------------------------------------------


async def test_unverifiable_turns_never_reach_the_store() -> None:
    store = RecordingStore()
    memory = ConversationMemory(store)
    viewer = Viewer(ALICE, frozenset({GENERAL}))
    answer = Answer("a", citations=(cite(ChannelRef("issues", 0), "issues"),))

    assert not await memory.remember(
        viewer, HERE, "q", answer, consulted=(), informed_by=NO_MEMORY
    )
    assert store.turns == []


async def test_a_turn_is_stored_under_the_viewers_own_person() -> None:
    store = RecordingStore()
    memory = ConversationMemory(store)
    viewer = Viewer(ALICE, frozenset({GENERAL}))

    assert await memory.remember(
        viewer, HERE, "q", Answer("the answer", citations=(cite(GENERAL),)), consulted=(ENG,),
        informed_by=NO_MEMORY,
    )
    assert store.turns == [(ALICE, HERE, "q", "the answer", frozenset({GENERAL, ENG}))]


async def test_recall_passes_the_viewer_and_the_bound() -> None:
    store = RecordingStore()
    memory = ConversationMemory(store, recent_turns=3)
    viewer = Viewer(ALICE, frozenset({GENERAL}))

    await memory.recall(viewer, HERE)
    assert store.recalls == [(viewer, HERE, 3)]


def test_a_non_positive_bound_is_refused() -> None:
    with pytest.raises(ValueError):
        ConversationMemory(RecordingStore(), recent_turns=0)


def test_consulted_has_no_default() -> None:
    """Citations-only provenance must be a choice, never an omission."""
    import inspect

    parameter = inspect.signature(ConversationMemory.remember).parameters["consulted"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_recall_requires_a_viewer() -> None:
    import inspect

    for method in (ConversationMemory.recall, RecordingStore.recall):
        parameter = inspect.signature(method).parameters["viewer"]
        assert parameter.default is inspect.Parameter.empty
