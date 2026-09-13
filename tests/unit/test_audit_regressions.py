"""Regressions for defects found by the adversarial audit.

Each test names the disclosure it prevents. They are grouped here rather than
scattered so the failure mode stays visible: every one of these passed review
as "obviously correct" code before the audit found the hole.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from chatmemory.adapters.discord.source import channel_of, is_ingestable, to_message
from chatmemory.app.disclosure import SUPPRESSED_ANSWER, enforce_audience
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.answers import Answer, Citation

PARENT, THREAD = 100, 555
T0 = datetime(2026, 9, 13, tzinfo=UTC)


# --- private threads ---------------------------------------------------


@dataclass
class FakeUser:
    id: int = 1
    bot: bool = False


@dataclass
class FakeChan:
    id: int
    parent_id: int | None = None
    type: Any = None
    _private: bool | None = None

    def is_private(self) -> bool:
        if self._private is None:
            raise AttributeError("partial object")
        return self._private


@dataclass
class FakeMsg:
    id: int = 1
    content: str = "the acquisition price is 40m"
    created_at: datetime = T0
    edited_at: datetime | None = None
    author: FakeUser = field(default_factory=FakeUser)
    mentions: tuple[FakeUser, ...] = ()
    channel: FakeChan = field(default_factory=lambda: FakeChan(PARENT))
    reference: Any = None


def test_public_thread_is_indexed_under_its_parent() -> None:
    msg = FakeMsg(channel=FakeChan(THREAD, parent_id=PARENT, _private=False))
    channel, thread_id = channel_of(msg)  # type: ignore[arg-type]
    assert channel == ChannelRef("discord", PARENT)
    assert thread_id == THREAD


def test_private_thread_is_not_indexed_at_all() -> None:
    """Its members are a narrower set than the parent's readers.

    Filing it under the parent would make a private conversation retrievable
    by everyone who can read the parent channel.
    """
    msg = FakeMsg(channel=FakeChan(THREAD, parent_id=PARENT, _private=True))
    channel, _ = channel_of(msg)  # type: ignore[arg-type]
    assert channel is None
    assert to_message(msg) is None  # type: ignore[arg-type]
    assert not is_ingestable(msg)  # type: ignore[arg-type]


def test_private_thread_detected_by_type_id() -> None:
    """discord.py exposes privacy differently depending on the object."""
    msg = FakeMsg(channel=FakeChan(THREAD, parent_id=PARENT, type=12, _private=False))
    assert channel_of(msg)[0] is None  # type: ignore[arg-type]


def test_undeterminable_privacy_is_treated_as_private() -> None:
    """A false positive costs one un-indexed thread; a false negative leaks."""
    msg = FakeMsg(channel=FakeChan(THREAD, parent_id=PARENT, _private=None))
    assert channel_of(msg)[0] is None  # type: ignore[arg-type]


def test_ordinary_channel_is_unaffected() -> None:
    channel, thread_id = channel_of(FakeMsg())  # type: ignore[arg-type]
    assert channel == ChannelRef("discord", PARENT)
    assert thread_id is None


# --- answer text scoping -----------------------------------------------

GENERAL = ChannelRef("discord", 1)
PRIVATE = ChannelRef("discord", 2)
ASKER = PersonRef("discord", 10)


def _public(readable: frozenset[ChannelRef]) -> Audience:
    return Audience(DeliveryMode.PUBLIC_CHANNEL, frozenset({ASKER}), readable, GENERAL)


def _cite(channel: ChannelRef) -> Citation:
    return Citation(channel, 1, "someone", "excerpt", "https://discord.com/1")


def test_answer_text_is_suppressed_when_evidence_is_dropped() -> None:
    """Filtering citations is not enough.

    The text was synthesized from evidence including the dropped source, so a
    paraphrase of a private conversation would go out with the citation
    removed -- a disclosure with the audit trail stripped off.
    """
    answer = Answer(
        "The acquisition price discussed in leadership was 40m.",
        citations=(_cite(GENERAL), _cite(PRIVATE)),
    )
    scoped = enforce_audience(
        answer, _public(frozenset({GENERAL})), Viewer(ASKER, frozenset({GENERAL, PRIVATE}))
    )
    assert scoped.answer.text == SUPPRESSED_ANSWER
    assert "40m" not in scoped.answer.text
    assert scoped.answer.citations == ()


def test_clean_answer_passes_through_untouched() -> None:
    answer = Answer("all good", citations=(_cite(GENERAL),))
    scoped = enforce_audience(
        answer, _public(frozenset({GENERAL})), Viewer(ASKER, frozenset({GENERAL}))
    )
    assert scoped.answer.text == "all good"
    assert scoped.answer.citations == answer.citations


def test_suppression_still_notifies_the_asker_privately() -> None:
    answer = Answer("secret detail", citations=(_cite(PRIVATE),))
    scoped = enforce_audience(
        answer, _public(frozenset({GENERAL})), Viewer(ASKER, frozenset({GENERAL, PRIVATE}))
    )
    assert scoped.should_notify_asker
    assert scoped.answer.withheld_channels == frozenset()
