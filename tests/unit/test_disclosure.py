"""The last guard before an answer reaches Discord."""

from __future__ import annotations

from chatmemory.app.disclosure import enforce_audience
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.answers import Answer, Citation

GENERAL = ChannelRef("discord", 1)
INFRA = ChannelRef("discord", 2)
LEADERSHIP = ChannelRef("discord", 3)
ASKER = PersonRef("discord", 10)


def cite(channel: ChannelRef, n: int) -> Citation:
    return Citation(channel, n, "someone", "excerpt", f"https://discord.com/{n}")


def public_audience(readable: frozenset[ChannelRef]) -> Audience:
    return Audience(
        mode=DeliveryMode.PUBLIC_CHANNEL,
        members=frozenset({ASKER}),
        readable_channels=readable,
        destination=GENERAL,
    )


def viewer(readable: frozenset[ChannelRef]) -> Viewer:
    return Viewer(person=ASKER, visible_channels=readable)


def test_out_of_audience_evidence_suppresses_the_whole_answer() -> None:
    """Dropping the citation alone is not enough.

    `text` was synthesized from evidence that included the dropped source, so
    trimming citations would ship a paraphrase of private content with its
    audit trail removed. The service is meant to scope at source; this guard
    firing means it did not, so the answer is suppressed rather than trimmed.
    """
    answer = Answer("...", citations=(cite(GENERAL, 1), cite(LEADERSHIP, 2)))
    scoped = enforce_audience(
        answer,
        public_audience(frozenset({GENERAL})),
        viewer(frozenset({GENERAL, LEADERSHIP})),
    )
    assert scoped.answer.source_channels == frozenset()
    assert scoped.answer.text != "..."


def test_asker_is_notified_about_evidence_they_could_have_seen() -> None:
    answer = Answer("...", citations=(cite(GENERAL, 1), cite(LEADERSHIP, 2)))
    scoped = enforce_audience(
        answer,
        public_audience(frozenset({GENERAL})),
        viewer(frozenset({GENERAL, LEADERSHIP})),
    )
    assert scoped.should_notify_asker
    assert scoped.withheld_from_audience == {LEADERSHIP}


def test_asker_is_not_told_about_evidence_they_also_cannot_see() -> None:
    """A notice about content the asker cannot read is itself a disclosure."""
    answer = Answer("...", citations=(cite(GENERAL, 1), cite(LEADERSHIP, 2)))
    scoped = enforce_audience(
        answer,
        public_audience(frozenset({GENERAL})),
        viewer(frozenset({GENERAL})),  # asker cannot read #leadership either
    )
    assert not scoped.should_notify_asker
    assert scoped.withheld_from_audience == frozenset()


def test_public_answer_never_signals_that_anything_was_withheld() -> None:
    answer = Answer("...", citations=(cite(GENERAL, 1), cite(LEADERSHIP, 2)))
    scoped = enforce_audience(
        answer,
        public_audience(frozenset({GENERAL})),
        viewer(frozenset({GENERAL, LEADERSHIP})),
    )
    assert scoped.answer.withheld_channels == frozenset()


def test_nothing_withheld_means_no_notice() -> None:
    answer = Answer("...", citations=(cite(GENERAL, 1),))
    scoped = enforce_audience(
        answer, public_audience(frozenset({GENERAL})), viewer(frozenset({GENERAL}))
    )
    assert not scoped.should_notify_asker
    assert scoped.answer.citations == answer.citations


def test_service_reported_withholding_is_carried_through() -> None:
    """The service may scope at source; this guard must not lose that signal."""
    answer = Answer("...", citations=(cite(GENERAL, 1),), withheld_channels=frozenset({INFRA}))
    scoped = enforce_audience(
        answer,
        public_audience(frozenset({GENERAL})),
        viewer(frozenset({GENERAL, INFRA})),
    )
    assert scoped.withheld_from_audience == {INFRA}


def test_public_evidence_is_a_subset_of_private_evidence() -> None:
    """The invariant from answer-disclosure, stated directly."""
    citations = (cite(GENERAL, 1), cite(INFRA, 2), cite(LEADERSHIP, 3))
    asker = viewer(frozenset({GENERAL, INFRA, LEADERSHIP}))

    private = enforce_audience(
        Answer("...", citations=citations),
        Audience(DeliveryMode.DIRECT_MESSAGE, frozenset({ASKER}), asker.visible_channels),
        asker,
    )
    public = enforce_audience(
        Answer("...", citations=citations), public_audience(frozenset({GENERAL})), asker
    )
    assert public.answer.source_channels <= private.answer.source_channels


def test_answer_is_marked_partial_when_evidence_was_dropped() -> None:
    answer = Answer("...", citations=(cite(GENERAL, 1), cite(LEADERSHIP, 2)))
    scoped = enforce_audience(
        answer,
        public_audience(frozenset({GENERAL})),
        viewer(frozenset({GENERAL, LEADERSHIP})),
    )
    assert scoped.answer.partial
