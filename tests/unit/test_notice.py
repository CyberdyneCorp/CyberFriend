"""The private notice: telling the asker a fuller answer exists.

Retrieval is scoped to asker INTERSECT audience *before* it runs, which is
what makes a public answer safe -- and also what makes the notice hard. The
answer carries no trace of what was never gathered, so the delivery guard has
nothing to report and `should_notify_asker` is structurally always False. A
lead asking in #general is never told that asking in a DM would get them
more. Something has to go and look; `WithheldRetrieval` is that something.

Two properties bound it, and both are asserted here. The public answer must
be identical whether or not anything was withheld -- otherwise the room reads
the difference and learns that private content exists. And the asker is told
only about channels they may read themselves, because a notice about content
they also cannot see is itself a disclosure.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from chatmemory.adapters.discord.acl import DiscordAclResolver, DiscordAudienceResolver
from chatmemory.app.ask import AskRequest, AskService
from chatmemory.app.disclosure import WithheldEvidenceProbe
from chatmemory.app.limits import RateLimiter
from chatmemory.app.reasoning.retrieval import WithheldRetrieval
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.search import RelevanceSource, SearchHit, SearchQuery
from chatmemory.ports.answers import Answer, Citation, Question
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

GENERAL, LEADERSHIP = 100, 300
LEAD, STAFF = 1, 3
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def ch(channel_id: int) -> ChannelRef:
    return ChannelRef("discord", channel_id)


def person(user_id: int) -> PersonRef:
    return PersonRef("discord", user_id)


def hit(window_id: int, channel: int, text: str) -> SearchHit:
    return SearchHit(
        window_id=window_id,
        channel=ch(channel),
        text=text,
        starts_at=NOW,
        ends_at=NOW,
        score=0.016,
        relevance_source=RelevanceSource.FUSED_RRF,
        message_ids=(window_id * 10,),
    )


def guild() -> FakeGuild:
    return FakeGuild(
        members=[
            FakeMember(LEAD, frozenset({"lead"})),
            FakeMember(STAFF, frozenset()),
        ],
        text_channels=[
            FakeChannel(GENERAL, public=True),
            FakeChannel(LEADERSHIP, allowed_roles=frozenset({"lead"})),
        ],
    )


class FakeSearch:
    """A backend that filters the way the real store's WHERE clause does.

    Filtering here rather than in the assertions is what lets these tests say
    the probe never *saw* a channel, rather than that something downstream
    discarded it.
    """

    def __init__(self, *hits: SearchHit, fail: bool = False) -> None:
        self._hits = hits
        self._fail = fail
        self.viewers: list[Viewer] = []

    async def search(self, viewer: Viewer, query: SearchQuery) -> Sequence[SearchHit]:
        if self._fail:
            raise TimeoutError("the database did not answer")
        self.viewers.append(viewer)
        return [h for h in self._hits if h.channel in viewer.visible_channels]

    async def thread_context(
        self, viewer: Viewer, platform_message_id: int, radius: int = 10
    ) -> Sequence[object]:
        return []

    async def list_channels(self, viewer: Viewer) -> Sequence[ChannelRef]:
        return []


class CorrectlyScopedAnswers:
    """An answer service that does its job: it cites only what the audience
    may read, so nothing reaches the delivery guard for it to drop.

    That is the whole point. With an upstream scoping failure the guard
    populates the withheld set by itself and the notice fires for the wrong
    reason; these tests must exercise the path where scoping worked.
    """

    async def answer(self, question: Question) -> Answer:
        citations = tuple(
            Citation(c, 10, "someone", "the deploy was rolled back", "https://discord.com/10")
            for c in sorted(question.audience.readable_channels, key=str)
        )
        return Answer("the deploy was rolled back at noon", citations=citations)


def build(search: FakeSearch, *, probe: bool = True) -> AskService:
    g = guild()
    indexed = (GENERAL, LEADERSHIP)
    return AskService(
        acl=DiscordAclResolver(g, indexed),
        audiences=DiscordAudienceResolver(g, indexed),
        answers=cast(Any, CorrectlyScopedAnswers()),
        limiter=RateLimiter(),
        withheld=WithheldRetrieval(cast(Any, search)) if probe else None,
    )


def corpus() -> FakeSearch:
    """Something relevant in the room, and something relevant outside it."""
    return FakeSearch(
        hit(1, GENERAL, "we rolled the deploy back at noon"),
        hit(2, LEADERSHIP, "the rollback was because of the incident"),
    )


async def ask_in_general(asks: AskService, user: int) -> Any:
    outcome = await asks.ask(
        AskRequest(person(user), "what happened with the deploy", ch(GENERAL), GENERAL)
    )
    assert outcome.scoped is not None
    return outcome.scoped


# --- the notice fires, for the right person -----------------------------


async def test_a_privileged_asker_is_told_privately_that_more_exists() -> None:
    """The lead may read #leadership; the room they asked in may not.

    This is the case the feature was built for and the case that could not
    happen: audience-scoped retrieval never gathered the #leadership window,
    so nothing downstream could notice it was missing.
    """
    scoped = await ask_in_general(build(corpus()), LEAD)

    assert scoped.should_notify_asker
    assert scoped.withheld_from_audience == {ch(LEADERSHIP)}


async def test_a_restricted_asker_is_told_nothing() -> None:
    """Staff cannot read #leadership either, so there is nothing to tell them
    -- and saying "there is more you cannot see" would be the disclosure."""
    scoped = await ask_in_general(build(corpus()), STAFF)

    assert not scoped.should_notify_asker
    assert scoped.withheld_from_audience == frozenset()


async def test_the_public_answer_is_the_same_either_way() -> None:
    """The room must not be able to tell the two apart.

    Compared whole rather than field by field: any difference at all -- text,
    citations, the partial flag -- is a channel through which the room learns
    that the asker is seeing a notice, and therefore that private content on
    the subject exists.
    """
    privileged = await ask_in_general(build(corpus()), LEAD)
    restricted = await ask_in_general(build(corpus()), STAFF)

    assert privileged.answer == restricted.answer
    assert privileged.answer.withheld_channels == frozenset()
    assert not privileged.answer.partial


async def test_the_notice_is_grounded_in_what_is_actually_there() -> None:
    """A lead always has channels the room does not. That alone is not news.

    Without the search behind it the notice would fire on every public
    question a lead ever asks, and be ignored within a week.
    """
    quiet = FakeSearch(hit(1, GENERAL, "we rolled the deploy back at noon"))
    scoped = await ask_in_general(build(quiet), LEAD)

    assert not scoped.should_notify_asker


# --- what the probe is allowed to do ------------------------------------


async def test_the_probe_looks_only_at_the_gap_and_only_as_the_asker() -> None:
    search = corpus()
    await ask_in_general(build(search), LEAD)

    assert search.viewers, "the probe never ran"
    for viewer in search.viewers:
        assert viewer.visible_channels == {ch(LEADERSHIP)}
        assert viewer.person == person(LEAD)


async def test_the_probe_returns_channels_and_nothing_else() -> None:
    """Structural, because the failure mode is not subtle: whatever this
    returns describes content the audience is not allowed to receive."""
    probe = WithheldRetrieval(cast(Any, corpus()))
    asker = Viewer(person(LEAD), frozenset({ch(GENERAL), ch(LEADERSHIP)}))
    audience = Audience(
        mode=DeliveryMode.PUBLIC_CHANNEL,
        members=frozenset({person(LEAD)}),
        readable_channels=frozenset({ch(GENERAL)}),
        destination=ch(GENERAL),
    )

    withheld = await probe.withheld_channels(asker, audience, "what happened")

    assert withheld == {ch(LEADERSHIP)}
    assert all(isinstance(c, ChannelRef) for c in withheld)


async def test_a_direct_message_probes_nothing() -> None:
    """A private audience is the asker's own visibility: the gap is empty by
    construction, so the query is not worth issuing."""
    search = corpus()
    outcome = await build(search).ask(
        AskRequest(person(LEAD), "what happened", None, location_id=LEAD)
    )

    assert outcome.scoped is not None
    assert not outcome.scoped.should_notify_asker
    assert search.viewers == []


async def test_a_failing_probe_costs_the_notice_and_not_the_answer() -> None:
    """The answer is the product; the notice is a courtesy."""
    scoped = await ask_in_general(build(FakeSearch(fail=True)), LEAD)

    assert scoped.answer.text == "the deploy was rolled back at noon"
    assert not scoped.should_notify_asker


async def test_a_backend_that_ignores_its_viewer_cannot_widen_the_notice() -> None:
    """Defence in depth: the probe intersects with the gap rather than
    trusting what came back, so a broken predicate names nothing new."""

    class LeakySearch(FakeSearch):
        async def search(self, viewer: Viewer, query: SearchQuery) -> Sequence[SearchHit]:
            self.viewers.append(viewer)
            return list(self._hits)

    secret = ch(999)
    leaky = LeakySearch(
        hit(2, LEADERSHIP, "the rollback was because of the incident"),
        hit(3, 999, "something in a channel nobody involved can read"),
    )
    probe = WithheldRetrieval(cast(Any, leaky))
    asker = Viewer(person(LEAD), frozenset({ch(GENERAL), ch(LEADERSHIP)}))
    audience = Audience(
        mode=DeliveryMode.PUBLIC_CHANNEL,
        members=frozenset({person(LEAD)}),
        readable_channels=frozenset({ch(GENERAL)}),
        destination=ch(GENERAL),
    )

    withheld = await probe.withheld_channels(asker, audience, "what happened")

    assert withheld == {ch(LEADERSHIP)}
    assert secret not in withheld


async def test_a_deployment_without_a_probe_answers_exactly_as_before() -> None:
    scoped = await ask_in_general(build(corpus(), probe=False), LEAD)

    assert scoped.answer.text == "the deploy was rolled back at noon"
    assert not scoped.should_notify_asker


def test_the_probe_satisfies_the_port_the_use_case_depends_on() -> None:
    """`AskService` depends on the protocol, never on this implementation."""
    probe: WithheldEvidenceProbe = WithheldRetrieval(cast(Any, FakeSearch()))
    assert probe is not None
