"""Conversation memory end to end: the bot's ask path over a real database.

tests/integration/test_memory_store.py proves the statements. This proves the
path the bot runs -- `AskService` -> `Conversations` -> `PostgresMemoryStore`
-- holds a conversation across a restart, erases it on /forget, stops
replaying a turn once its channel is revoked, and summarises in the
background through the real replace-with-summary statement.

The Discord side is the unit fakes; the model is a scripted answer service.
Everything between them is what production runs.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.discord.acl import DiscordAclResolver, DiscordAudienceResolver
from chatmemory.adapters.store.memory_postgres import PostgresMemoryStore
from chatmemory.adapters.store.retention_sql import PostgresRetentionStore
from chatmemory.app.ask import AskRequest, AskService, ForgetRequest
from chatmemory.app.conversation import Conversations, ConversationSummariser, MemoryPolicy
from chatmemory.app.limits import RateLimiter
from chatmemory.app.optout import OptOutService
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.ports.answers import Answer, Citation, Question
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember
from tests.unit.test_conversation_memory import SummaryModel

pytestmark = pytest.mark.asyncio

GENERAL, LEADERSHIP = 600, 601
LEAD, STAFF = 21, 22


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


class Answers:
    """Cites and consults every channel the audience may read."""

    def __init__(self) -> None:
        self.seen: list[Question] = []

    async def answer(self, question: Question) -> Answer:
        self.seen.append(question)
        readable = sorted(question.audience.readable_channels, key=str)
        return Answer(
            f"answer to {question.text}",
            citations=tuple(Citation(c, 1, "x", "y", "https://z") for c in readable),
            consulted_channels=frozenset(readable),
        )


def process(
    engine: AsyncEngine,
    g: FakeGuild | None = None,
    model: SummaryModel | None = None,
    policy: MemoryPolicy | None = None,
) -> tuple[AskService, Answers, Conversations]:
    """One bot process: a fresh object graph over the same database."""
    fake = g or guild()
    indexed = (GENERAL, LEADERSHIP)
    store = PostgresMemoryStore(engine)
    rule = policy or MemoryPolicy()
    memory = Conversations(
        store,
        ConversationSummariser(store, model or SummaryModel(), rule),  # type: ignore[arg-type]
        rule,
    )
    answers = Answers()
    asks = AskService(
        acl=DiscordAclResolver(fake, indexed),
        audiences=DiscordAudienceResolver(fake, indexed),
        answers=answers,
        limiter=RateLimiter(),
        conversations=memory,
    )
    return asks, answers, memory


def in_general(who: int, words: str) -> AskRequest:
    return AskRequest(person(who), words, ch(GENERAL), location_id=GENERAL)


def in_dm(who: int, words: str) -> AskRequest:
    return AskRequest(person(who), words, None, location_id=900 + who)


# --- 7.1 / 7.2 a follow-up resolves, across a restart --------------------


async def test_a_follow_up_is_interpreted_with_the_earlier_turn_after_a_restart(
    clean: AsyncEngine,
) -> None:
    before, _, _ = process(clean)
    await before.ask(in_general(LEAD, "what did we decide about the deploy freeze?"))

    after, answers, _ = process(clean)  # a new process: nothing held in memory
    await after.ask(in_general(LEAD, "and last month?"))

    [follow_up] = answers.seen
    assert [t.question for t in follow_up.memory.turns] == [
        "what did we decide about the deploy freeze?"
    ]
    assert follow_up.memory.turns[0].answer.startswith("answer to what did we decide")


async def test_people_in_one_channel_and_one_persons_dm_stay_apart(clean: AsyncEngine) -> None:
    asks, answers, _ = process(clean)
    await asks.ask(in_general(LEAD, "lead in general"))
    await asks.ask(in_dm(LEAD, "lead in a dm"))
    await asks.ask(in_general(STAFF, "staff in general"))
    await asks.ask(in_general(LEAD, "lead again in general"))

    staff, lead_again = answers.seen[2], answers.seen[3]
    assert staff.memory.empty
    assert [t.question for t in lead_again.memory.turns] == ["lead in general"]


# --- 7.3 /forget ------------------------------------------------------------


async def test_after_forget_the_follow_up_no_longer_resolves(clean: AsyncEngine) -> None:
    asks, answers, _ = process(clean)
    await asks.ask(in_general(LEAD, "what did we decide about the deploy freeze?"))
    await asks.ask(in_general(STAFF, "someone else's question"))

    purge = await asks.forget(ForgetRequest(person(LEAD), in_general(LEAD, "").location))
    assert purge is not None and purge.turns == 1

    await asks.ask(in_general(LEAD, "and last month?"))
    assert answers.seen[-1].memory.empty
    await asks.ask(in_general(STAFF, "and theirs?"))
    assert [t.question for t in answers.seen[-1].memory.turns] == ["someone else's question"]


# --- permissions and opt-out ------------------------------------------------


async def test_a_revoked_channel_withholds_the_turn_from_the_next_question(
    clean: AsyncEngine,
) -> None:
    lead, _, _ = process(clean)
    await lead.ask(in_dm(LEAD, "what did leadership decide?"))

    demoted, answers, _ = process(clean, guild(lead_roles=frozenset()))
    await demoted.ask(in_dm(LEAD, "and after that?"))

    assert answers.seen[-1].memory.empty


async def test_an_opted_out_person_is_answered_and_not_remembered(clean: AsyncEngine) -> None:
    asks, answers, _ = process(clean)
    await asks.ask(in_general(LEAD, "before opting out"))
    await OptOutService(PostgresRetentionStore(clean)).opt_out(person(LEAD), "asked")

    outcome = await asks.ask(in_general(LEAD, "after opting out"))
    await asks.ask(in_general(LEAD, "and again"))

    assert outcome.answered
    assert answers.seen[-1].memory.empty
    async with clean.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM conversation_turn"))).scalar()
    assert count == 0


# --- summarisation through the real statement ---------------------------------


async def test_a_long_conversation_is_summarised_in_the_database(clean: AsyncEngine) -> None:
    model = SummaryModel("they were asking about the deploy freeze")
    asks, answers, memory = process(
        clean, model=model, policy=MemoryPolicy(recent_turns=2, summarise_after_turns=3)
    )
    for n in range(4):
        await asks.ask(in_general(LEAD, f"question {n}"))
    await memory.drain()

    await asks.ask(in_general(LEAD, "and now?"))
    recalled = answers.seen[-1].memory
    assert [s.text for s in recalled.summaries] == ["they were asking about the deploy freeze"]
    assert [t.question for t in recalled.turns] == ["question 2", "question 3"]
    async with clean.connect() as conn:
        covered = (
            await conn.execute(text("SELECT covered_channel_ids FROM conversation_summary"))
        ).scalar()
    assert covered == [GENERAL]
