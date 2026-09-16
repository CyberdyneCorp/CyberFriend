"""Conversation memory as the ask path uses it: keyed, re-checked, condensed, erased.

The store's own statements are tested against Postgres in
tests/integration/test_memory_store.py. These drive `AskService` -- the object
the bot calls -- over a fake store that applies the same rule the SQL does, so
what is checked here is the part the store cannot check: that the ask path
reads with the right key, under the right view, and writes the right
provenance.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from chatmemory.adapters.discord.acl import DiscordAclResolver, DiscordAudienceResolver
from chatmemory.adapters.discord.bot import (
    FORGET_EVERYWHERE,
    FORGET_HERE,
    CyberFriendClient,
    forget_request,
)
from chatmemory.app.ask import AskRequest, AskService, ForgetRequest
from chatmemory.app.conversation import (
    SUMMARY_SYSTEM,
    Conversations,
    ConversationSummariser,
    MemoryPolicy,
    MemoryRetention,
)
from chatmemory.app.disclosure import SUPPRESSED_ANSWER
from chatmemory.app.limits import RateLimiter
from chatmemory.app.reasoning.ports import JsonCompletion, TextCompletion
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.ports.answers import Answer, AskerProfile, Citation, Question
from chatmemory.ports.memory import (
    ConversationLocation,
    MemoryPurge,
    MemoryStore,
    Recollection,
    RememberedSummary,
    RememberedTurn,
)
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

GENERAL, LEADERSHIP = 100, 300
LEAD, STAFF = 1, 3
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def ch(cid: int) -> ChannelRef:
    return ChannelRef("discord", cid)


def person(uid: int) -> PersonRef:
    return PersonRef("discord", uid)


def guild(lead_roles: frozenset[str] = frozenset({"lead"})) -> FakeGuild:
    return FakeGuild(
        members=[FakeMember(LEAD, lead_roles), FakeMember(STAFF, frozenset())],
        text_channels=[
            FakeChannel(GENERAL, public=True),
            FakeChannel(LEADERSHIP, allowed_roles=frozenset({"lead"})),
        ],
    )


# --- a store that applies the SQL's rule --------------------------------


@dataclass
class _Turn:
    turn_id: int
    person: PersonRef
    location: ConversationLocation
    question: str
    answer: str
    channels: frozenset[int]
    at: datetime


@dataclass
class _Summary:
    person: PersonRef
    location: ConversationLocation
    text: str
    channels: frozenset[int]
    through: int
    starts_at: datetime


@dataclass
class FakeMemoryStore:
    """The permission rule of `memory_sql`, in Python.

    Readability is containment of the recorded channel ids in the viewer's
    current ids, for turns and for summaries alike; person and location are
    both part of the key. Anything looser here would let these tests pass
    over an ask path that reads with the wrong key.
    """

    turns: list[_Turn] = field(default_factory=list)
    summaries: list[_Summary] = field(default_factory=list)
    recalls: list[tuple[Viewer, ConversationLocation, int]] = field(default_factory=list)
    fail_recall: bool = False
    clock: datetime = NOW
    last_id: int = 0

    async def record_turn(
        self,
        person: PersonRef,
        location: ConversationLocation,
        question: str,
        answer: str,
        source_channels: frozenset[ChannelRef],
    ) -> bool:
        # Monotonic, like the table's sequence: ids are never reused after a
        # summary deletes the turns that held them.
        self.last_id += 1
        self.turns.append(
            _Turn(
                turn_id=self.last_id,
                person=person,
                location=location,
                question=question,
                answer=answer,
                channels=frozenset(c.platform_channel_id for c in source_channels),
                at=self.clock,
            )
        )
        return True

    async def recall(
        self, viewer: Viewer, location: ConversationLocation, turn_limit: int
    ) -> Recollection:
        self.recalls.append((viewer, location, turn_limit))
        if self.fail_recall:
            raise ConnectionError("database went away")
        readable = frozenset(c.platform_channel_id for c in viewer.visible_channels)
        turns = [
            t
            for t in self.turns
            if t.person == viewer.person and t.location == location and t.channels <= readable
        ][-turn_limit:]
        summaries = [
            s
            for s in self.summaries
            if s.person == viewer.person and s.location == location and s.channels <= readable
        ]
        return Recollection(
            summaries=tuple(
                RememberedSummary(s.text, s.through, s.starts_at, _refs(s.channels))
                for s in summaries
            ),
            turns=tuple(
                RememberedTurn(t.turn_id, t.question, t.answer, t.at, _refs(t.channels))
                for t in turns
            ),
        )

    async def record_summary(
        self, person: PersonRef, location: ConversationLocation, text: str, through_turn_id: int
    ) -> bool:
        mine = [t for t in self.turns if t.person == person and t.location == location]
        replaced = [t for t in mine if t.turn_id <= through_turn_id]
        old = [s for s in self.summaries if s.person == person and s.location == location]
        if not replaced and not old:
            return False
        channels = frozenset().union(*(t.channels for t in replaced), *(s.channels for s in old))
        self.turns = [t for t in self.turns if t not in replaced]
        self.summaries = [s for s in self.summaries if s not in old]
        self.summaries.append(
            _Summary(person, location, text, channels, through_turn_id, self.clock)
        )
        return True

    async def forget(
        self, person: PersonRef, location: ConversationLocation | None
    ) -> MemoryPurge:
        def gone(owner: PersonRef, where: ConversationLocation) -> bool:
            return owner == person and (location is None or where == location)

        turns = [t for t in self.turns if gone(t.person, t.location)]
        summaries = [s for s in self.summaries if gone(s.person, s.location)]
        self.turns = [t for t in self.turns if t not in turns]
        self.summaries = [s for s in self.summaries if s not in summaries]
        return MemoryPurge(turns=len(turns), summaries=len(summaries))

    async def purge_before(self, cutoff: datetime) -> MemoryPurge:
        turns = [t for t in self.turns if t.at < cutoff]
        summaries = [s for s in self.summaries if s.starts_at < cutoff]
        self.turns = [t for t in self.turns if t not in turns]
        self.summaries = [s for s in self.summaries if s not in summaries]
        return MemoryPurge(turns=len(turns), summaries=len(summaries))


def _refs(ids: frozenset[int]) -> frozenset[ChannelRef]:
    return frozenset(ch(i) for i in ids)


def _conforms(store: FakeMemoryStore) -> MemoryStore:
    return store


class SummaryModel:
    """The cheap model. Optionally held until a test lets it answer."""

    def __init__(self, text: str = "they asked about the deploy freeze") -> None:
        self.text = text
        self.prompts: list[tuple[str, str]] = []
        self.release = asyncio.Event()
        self.release.set()

    async def complete_text(self, system: str, user: str) -> TextCompletion:
        self.prompts.append((system, user))
        await self.release.wait()
        return TextCompletion(text=self.text)

    async def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], schema_name: str
    ) -> JsonCompletion:  # pragma: no cover - the summariser never asks for JSON
        raise AssertionError("the summariser writes prose")

    async def complete_with_tools(self, *args: object) -> object:  # pragma: no cover
        raise AssertionError("the summariser calls no tools")


def conversations(
    store: FakeMemoryStore,
    model: SummaryModel | None = None,
    policy: MemoryPolicy | None = None,
) -> Conversations:
    rule = policy or MemoryPolicy(recent_turns=2, summarise_after_turns=3)
    return Conversations(
        store,
        ConversationSummariser(store, model or SummaryModel(), rule),  # type: ignore[arg-type]
        rule,
    )


# --- the answer service the ask path drives ------------------------------


class RecordingAnswers:
    """Answers from the audience's channels, and says it consulted them."""

    def __init__(self, consulted: frozenset[ChannelRef] | None = None, text: str = "") -> None:
        self.seen: list[Question] = []
        self._consulted = consulted
        self._text = text

    async def answer(self, question: Question) -> Answer:
        self.seen.append(question)
        readable = sorted(question.audience.readable_channels, key=str)
        consulted = (
            frozenset(readable) if self._consulted is None else self._consulted
        )
        return Answer(
            self._text or f"answer to {question.text}",
            citations=tuple(
                Citation(c, 1, "someone", "text", "https://discord.com/x") for c in readable
            ),
            consulted_channels=consulted,
        )


class Profiles:
    def __init__(self) -> None:
        self.asked_for: list[PersonRef] = []

    async def resolve_profile(self, who: PersonRef) -> AskerProfile | None:
        self.asked_for.append(who)
        return AskerProfile(who, f"member {who.platform_user_id}", None, ("engineering",))


def service(
    store: FakeMemoryStore,
    answers: object | None = None,
    g: FakeGuild | None = None,
    memory: Conversations | None = None,
    profiles: Profiles | None = None,
) -> tuple[AskService, RecordingAnswers]:
    fake = g or guild()
    indexed = (GENERAL, LEADERSHIP)
    recorder = answers or RecordingAnswers()
    return (
        AskService(
            acl=DiscordAclResolver(fake, indexed),
            audiences=DiscordAudienceResolver(fake, indexed),
            answers=recorder,  # type: ignore[arg-type]
            limiter=RateLimiter(),
            conversations=memory or conversations(store),
            profiles=profiles,
        ),
        recorder,  # type: ignore[return-value]
    )


def in_general(who: int, text: str) -> AskRequest:
    return AskRequest(person(who), text, ch(GENERAL), location_id=GENERAL)


def in_dm(who: int, text: str, location_id: int = GENERAL) -> AskRequest:
    # Deliberately the channel's id: the kind alone must keep them apart.
    return AskRequest(person(who), text, None, location_id=location_id)


def remembered_questions(question: Question) -> list[str]:
    return [t.question for t in question.memory.turns]


# --- keyed by person and location ---------------------------------------


async def test_a_follow_up_carries_the_askers_own_earlier_turn() -> None:
    asks, answers = service(FakeMemoryStore())
    first = "what did we decide about the deploy freeze?"
    await asks.ask(in_general(LEAD, first))
    await asks.ask(in_general(LEAD, "and last month?"))

    follow_up = answers.seen[-1]
    assert remembered_questions(follow_up) == [first]
    assert follow_up.memory.turns[0].answer == f"answer to {first}"


async def test_two_people_in_one_channel_never_share_a_conversation() -> None:
    asks, answers = service(FakeMemoryStore())
    await asks.ask(in_general(LEAD, "lead's question"))
    await asks.ask(in_general(STAFF, "staff's question"))
    await asks.ask(in_general(STAFF, "staff's follow-up"))
    await asks.ask(in_general(LEAD, "lead's follow-up"))

    staff_first, staff_follow_up, lead_follow_up = answers.seen[1], answers.seen[2], answers.seen[3]
    assert staff_first.memory.empty, "staff's first question inherited the lead's turn"
    assert remembered_questions(staff_follow_up) == ["staff's question"]
    assert remembered_questions(lead_follow_up) == ["lead's question"]


async def test_a_direct_message_never_informs_a_channel_answer() -> None:
    asks, answers = service(FakeMemoryStore())
    await asks.ask(in_dm(LEAD, "something private"))
    await asks.ask(in_general(LEAD, "asked in the channel"))
    await asks.ask(in_dm(LEAD, "back in the DM"))

    assert answers.seen[1].memory.empty
    assert remembered_questions(answers.seen[2]) == ["something private"]


async def test_the_recall_is_judged_by_what_the_room_can_read() -> None:
    """A turn from the lead's DM drew on #leadership. Asked in #general, the
    view memory is judged by is the asker narrowed to the room."""
    store = FakeMemoryStore()
    asks, _ = service(store)
    await asks.ask(in_general(LEAD, "q"))

    viewer, location, _ = store.recalls[-1]
    assert viewer.person == person(LEAD)
    assert viewer.visible_channels == {ch(GENERAL)}
    assert location == ConversationLocation("discord", GENERAL, direct=False)


# --- provenance, and what is written ------------------------------------


async def test_a_turn_is_stored_with_its_answer_and_the_channels_it_drew_on() -> None:
    store = FakeMemoryStore()
    consulted = frozenset({ch(GENERAL), ch(LEADERSHIP)})
    asks, _ = service(store, RecordingAnswers(consulted=consulted))
    await asks.ask(in_dm(LEAD, "what happened?"))

    [turn] = store.turns
    assert turn.person == person(LEAD)
    assert turn.answer == "answer to what happened?"
    # Every channel the run consulted, not just the ones it cited.
    assert turn.channels == {GENERAL, LEADERSHIP}


async def test_an_answer_with_unknown_provenance_is_not_remembered() -> None:
    class Unknown(RecordingAnswers):
        async def answer(self, question: Question) -> Answer:
            self.seen.append(question)
            return Answer("from somewhere")

    store = FakeMemoryStore()
    asks, _ = service(store, Unknown())
    outcome = await asks.ask(in_general(LEAD, "q"))

    assert outcome.answered
    assert store.turns == []


async def test_a_suppressed_reply_is_remembered_as_what_was_delivered() -> None:
    """The guard suppressed prose built on #leadership. The unsuppressed text
    must not come back later as memory, and the channel it drew on must still
    count against the turn."""

    class Leaky(RecordingAnswers):
        async def answer(self, question: Question) -> Answer:
            self.seen.append(question)
            return Answer(
                "the leadership offsite is cancelled",
                citations=(Citation(ch(LEADERSHIP), 1, "x", "y", "https://z"),),
                consulted_channels=frozenset({ch(LEADERSHIP)}),
            )

    store = FakeMemoryStore()
    asks, _ = service(store, Leaky())
    outcome = await asks.ask(in_general(LEAD, "anything new?"))

    assert outcome.scoped is not None and outcome.scoped.answer.text == SUPPRESSED_ANSWER
    [turn] = store.turns
    assert turn.answer == SUPPRESSED_ANSWER
    assert "offsite" not in turn.answer
    assert turn.channels == {LEADERSHIP}


async def test_a_turn_on_a_revoked_channel_is_not_recalled() -> None:
    store = FakeMemoryStore()
    asks, answers = service(store, RecordingAnswers(consulted=frozenset({ch(LEADERSHIP)})))
    await asks.ask(in_dm(LEAD, "what did leadership decide?", location_id=LEAD))

    demoted, answers2 = service(
        store, RecordingAnswers(consulted=frozenset()), g=guild(lead_roles=frozenset())
    )
    await demoted.ask(in_dm(LEAD, "and then?", location_id=LEAD))

    assert answers2.seen[-1].memory.empty


class Restating(RecordingAnswers):
    """Cites only what it was told to, and repeats whatever memory it was shown.

    The model a follow-up reaches: it resolves "that" from the fenced memory
    and says so, while the evidence it retrieved this time is somewhere else.
    """

    def __init__(self, consulted: frozenset[ChannelRef]) -> None:
        super().__init__(consulted=consulted)

    async def answer(self, question: Question) -> Answer:
        self.seen.append(question)
        shown = [s.text for s in question.memory.summaries]
        shown += [t.answer for t in question.memory.turns]
        assert self._consulted is not None
        return Answer(
            f"about that ({'; '.join(shown)}): #general only mentions a Friday lunch",
            citations=tuple(
                Citation(c, 1, "x", "y", "https://z")
                for c in self._consulted
                if c.platform == "discord"
            ),
            consulted_channels=self._consulted,
        )


LAYOFF = "leadership decided to lay off Carol on Friday"
WEB = ChannelRef("web", 0)


async def _ask_after_demotion(store: FakeMemoryStore) -> Recollection:
    demoted, answers = service(
        store, RecordingAnswers(consulted=frozenset()), g=guild(lead_roles=frozenset())
    )
    await demoted.ask(in_dm(LEAD, "remind me what we discussed", location_id=LEAD))
    return answers.seen[-1].memory


def _mentions_layoff(memory: Recollection) -> bool:
    return any("Carol" in s.text for s in memory.summaries) or any(
        "Carol" in t.answer for t in memory.turns
    )


@pytest.mark.parametrize(
    "follow_up_consulted",
    [frozenset({ch(GENERAL)}), frozenset({WEB})],
    ids=["general-only", "web-only"],
)
async def test_a_turn_restating_memory_inherits_the_memorys_channels(
    follow_up_consulted: frozenset[ChannelRef],
) -> None:
    """Regression: provenance must carry forward through memory.

    The follow-up's own evidence is #general (or the web, which is no channel
    at all), but its answer restates the remembered #leadership answer. Stored
    with only its own evidence, it came back after the lead lost #leadership --
    and a web-only follow-up, stored with no channels, came back after they
    lost everything.
    """
    store = FakeMemoryStore()
    asks, _ = service(store, RecordingAnswers(consulted=frozenset({ch(LEADERSHIP)}), text=LAYOFF))
    await asks.ask(in_dm(LEAD, "what did leadership decide?", location_id=LEAD))

    follow, answers = service(store, Restating(follow_up_consulted))
    await follow.ask(in_dm(LEAD, "did #general talk about that?", location_id=LEAD))
    assert answers.seen[-1].memory.turns, "the follow-up was not shown the first turn"
    restated = store.turns[-1]
    assert "Carol" in restated.answer
    assert LEADERSHIP in restated.channels

    assert not _mentions_layoff(await _ask_after_demotion(store))


async def test_a_turn_restating_a_summary_inherits_what_the_summary_covered() -> None:
    store = FakeMemoryStore()
    here = ConversationLocation("discord", LEAD, direct=True)
    store.summaries.append(
        _Summary(person(LEAD), here, LAYOFF, frozenset({LEADERSHIP}), 0, NOW)
    )

    follow, answers = service(store, Restating(frozenset({ch(GENERAL)})))
    await follow.ask(in_dm(LEAD, "did #general talk about that?", location_id=LEAD))
    assert answers.seen[-1].memory.summaries, "the follow-up was not shown the summary"
    assert store.turns[-1].channels == {GENERAL, LEADERSHIP}

    assert not _mentions_layoff(await _ask_after_demotion(store))


async def test_a_memory_store_that_fails_costs_context_not_the_answer() -> None:
    store = FakeMemoryStore(fail_recall=True)
    asks, answers = service(store)
    outcome = await asks.ask(in_general(LEAD, "q"))

    assert outcome.answered
    assert answers.seen[-1].memory.empty


# --- the asker's profile -------------------------------------------------


async def test_only_the_askers_own_profile_is_looked_up_and_attached() -> None:
    profiles = Profiles()
    asks, answers = service(FakeMemoryStore(), profiles=profiles)
    await asks.ask(in_general(LEAD, "what did I say about the migration?"))
    await asks.ask(in_general(STAFF, "and me?"))

    assert profiles.asked_for == [person(LEAD), person(STAFF)]
    for question, who in zip(answers.seen, (LEAD, STAFF), strict=True):
        assert question.asker_profile is not None
        assert question.asker_profile.person == person(who)


# --- summarisation --------------------------------------------------------


async def test_a_long_conversation_is_summarised_keeping_the_recent_turns() -> None:
    store = FakeMemoryStore()
    model = SummaryModel()
    memory = conversations(store, model, MemoryPolicy(recent_turns=2, summarise_after_turns=3))
    asks, answers = service(store, memory=memory)
    for n in range(4):
        await asks.ask(in_general(LEAD, f"question {n}"))
    await memory.drain()

    assert [t.question for t in store.turns] == ["question 2", "question 3"]
    [summary] = store.summaries
    assert summary.text == "they asked about the deploy freeze"
    # The union of what it replaced, computed from the rows, not supplied.
    assert summary.channels == {GENERAL}
    # The model saw the older turns, fenced, and was told they are data.
    [(system, user)] = model.prompts
    assert system == SUMMARY_SYSTEM
    assert "question 0" in user and "question 1" in user and "question 3" not in user
    assert user.startswith("<<<MEMORY fence=")

    await asks.ask(in_general(LEAD, "question 4"))
    follow_up = answers.seen[-1].memory
    assert [s.text for s in follow_up.summaries] == ["they asked about the deploy freeze"]


async def test_a_question_never_waits_on_summarisation() -> None:
    store = FakeMemoryStore()
    model = SummaryModel()
    model.release.clear()  # the cheap model hangs
    memory = conversations(store, model, MemoryPolicy(recent_turns=1, summarise_after_turns=2))
    asks, _ = service(store, memory=memory)
    for n in range(3):
        await asks.ask(in_general(LEAD, f"q{n}"))

    # The third ask triggered a summary whose model has not answered, and the
    # ask returned anyway. Another question is answered while it is pending.
    outcome = await asyncio.wait_for(asks.ask(in_general(LEAD, "q3")), timeout=1)
    assert outcome.answered
    assert store.summaries == []

    model.release.set()
    await memory.drain()
    assert len(store.summaries) == 1


async def test_a_failing_summary_leaves_the_turns_in_place() -> None:
    class Broken(SummaryModel):
        async def complete_text(self, system: str, user: str) -> TextCompletion:
            raise TimeoutError("cheap model down")

    store = FakeMemoryStore()
    memory = conversations(store, Broken(), MemoryPolicy(recent_turns=1, summarise_after_turns=2))
    asks, _ = service(store, memory=memory)
    for n in range(3):
        assert (await asks.ask(in_general(LEAD, f"q{n}"))).answered
    await memory.drain()

    assert len(store.turns) == 3
    assert store.summaries == []


async def test_a_summary_does_not_contain_text_from_a_revoked_channel() -> None:
    """Unreadable turns are left out of the text but counted in provenance."""
    store = FakeMemoryStore()
    here = ConversationLocation("discord", LEAD, direct=True)
    await store.record_turn(person(LEAD), here, "secret one", "about leadership",
                            frozenset({ch(LEADERSHIP)}))
    for n in range(3):
        await store.record_turn(person(LEAD), here, f"open {n}", "fine", frozenset({ch(GENERAL)}))
    model = SummaryModel()
    summariser = ConversationSummariser(
        store, model, MemoryPolicy(recent_turns=1, summarise_after_turns=2)  # type: ignore[arg-type]
    )

    staff_view = Viewer(person(LEAD), frozenset({ch(GENERAL)}))
    assert await summariser.summarise(staff_view, here)

    [(_, user)] = model.prompts
    assert "secret one" not in user and "about leadership" not in user
    [summary] = store.summaries
    assert summary.channels == {GENERAL, LEADERSHIP}
    assert (await store.recall(staff_view, here, 10)).summaries == ()


def test_the_bound_must_leave_something_older_to_summarise() -> None:
    with pytest.raises(ValueError):
        MemoryPolicy(recent_turns=6, summarise_after_turns=6)


# --- forgetting and retention ---------------------------------------------


async def test_forget_here_erases_this_conversation_and_nothing_else() -> None:
    store = FakeMemoryStore()
    asks, answers = service(store)
    await asks.ask(in_general(LEAD, "channel question"))
    await asks.ask(in_dm(LEAD, "dm question"))
    await asks.ask(in_general(STAFF, "someone else's"))

    purge = await asks.forget(ForgetRequest(person(LEAD), in_general(LEAD, "").location))

    assert purge == MemoryPurge(turns=1, summaries=0)
    assert sorted(t.question for t in store.turns) == ["dm question", "someone else's"]
    await asks.ask(in_general(LEAD, "and then?"))
    assert answers.seen[-1].memory.empty


async def test_forget_everywhere_erases_only_the_requesters_history() -> None:
    store = FakeMemoryStore()
    asks, _ = service(store)
    await asks.ask(in_general(LEAD, "channel question"))
    await asks.ask(in_dm(LEAD, "dm question"))
    await asks.ask(in_general(STAFF, "someone else's"))

    purge = await asks.forget(ForgetRequest(person(LEAD), None))

    assert purge is not None and purge.turns == 2
    assert [t.question for t in store.turns] == ["someone else's"]


async def test_forget_without_memory_says_so_rather_than_succeeding() -> None:
    fake = guild()
    asks = AskService(
        acl=DiscordAclResolver(fake, (GENERAL,)),
        audiences=DiscordAudienceResolver(fake, (GENERAL,)),
        answers=RecordingAnswers(),
        limiter=RateLimiter(),
    )
    assert await asks.forget(ForgetRequest(person(LEAD), None)) is None


@pytest.mark.parametrize(
    ("channel_id", "in_guild", "destination"),
    [(GENERAL, True, ch(GENERAL)), (777, False, None)],
)
def test_forget_names_the_location_ask_remembered_under(
    channel_id: int, in_guild: bool, destination: ChannelRef | None
) -> None:
    asked = AskRequest(person(LEAD), "q", destination, location_id=channel_id)
    request = forget_request(
        person(LEAD), channel_id=channel_id, in_guild=in_guild, everywhere=False
    )
    assert request.location == asked.location
    everywhere = forget_request(
        person(LEAD), channel_id=channel_id, in_guild=in_guild, everywhere=True
    )
    assert everywhere.location is None


@dataclass
class _Response:
    deferred: list[bool] = field(default_factory=list)

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.deferred.append(ephemeral)


@dataclass
class _Followup:
    sent: list[tuple[str, bool]] = field(default_factory=list)

    async def send(self, content: str, *, ephemeral: bool = False) -> None:
        self.sent.append((content, ephemeral))


@dataclass
class _User:
    id: int


@dataclass
class _Interaction:
    user: _User
    channel_id: int | None
    guild_id: int | None
    response: _Response = field(default_factory=_Response)
    followup: _Followup = field(default_factory=_Followup)


@pytest.mark.parametrize("scope", [FORGET_HERE, FORGET_EVERYWHERE])
async def test_the_registered_forget_command_erases_the_callers_history(scope: str) -> None:
    from discord import app_commands

    store = FakeMemoryStore()
    asks, _ = service(store)
    await asks.ask(in_general(LEAD, "mine"))
    await asks.ask(in_general(STAFF, "theirs"))

    command = CyberFriendClient(asks, guild_id=1)._build_forget_command()
    assert command.name == "forget"
    interaction = _Interaction(_User(LEAD), channel_id=GENERAL, guild_id=1)
    await command.callback(interaction, scope=app_commands.Choice(name=scope, value=scope))

    assert [t.question for t in store.turns] == ["theirs"]
    [(note, ephemeral)] = interaction.followup.sent
    # Private: erasing what you asked is nobody else's business.
    assert ephemeral and interaction.response.deferred == [True]
    assert "forgotten" in note


async def test_retention_deletes_what_is_past_the_window() -> None:
    store = FakeMemoryStore()
    here = ConversationLocation("discord", GENERAL, direct=False)
    store.clock = NOW - timedelta(days=31)
    await store.record_turn(person(LEAD), here, "old", "a", frozenset())
    store.clock = NOW - timedelta(days=1)
    await store.record_turn(person(LEAD), here, "recent", "a", frozenset())

    purge = await MemoryRetention(store, timedelta(days=30)).sweep(NOW)

    assert purge.turns == 1
    assert [t.question for t in store.turns] == ["recent"]
