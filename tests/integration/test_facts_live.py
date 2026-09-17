"""Personal facts through the ask path, against a real database.

The unit tests drive `AskService` over a fake store. These run the same
requests over the store the bot process builds -- `build_personal_facts` on a
live engine -- so a statement that keyed on the wrong person, or a reply that
read more than the asker's own row, fails here rather than in a channel.
"""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.discord.acl import DiscordAclResolver, DiscordAudienceResolver
from chatmemory.app.ask import (
    EMAIL_CHANNEL_NOTE,
    FACT_ABOUT_SOMEONE_ELSE,
    FACT_OTHERS_REFUSED,
    AskRequest,
    AskService,
    ForgetRequest,
)
from chatmemory.app.limits import RateLimiter
from chatmemory.app.reasoning.stages import SYNTHESIS_SYSTEM, prompt_context, with_context
from chatmemory.composition import build_personal_facts
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.ports.answers import Answer, Question
from tests.unit.fakes import FakeChannel, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio

GENERAL = 700
LEO, JOAO = PersonRef("discord", 2001), PersonRef("discord", 2002)
IN_GENERAL = ChannelRef("discord", GENERAL)
EMAIL = "leo@example.com"


class Answers:
    def __init__(self) -> None:
        self.users: list[str] = []

    async def answer(self, question: Question) -> Answer:
        _, user = with_context(SYNTHESIS_SYSTEM, question.text, prompt_context(question))
        self.users.append(user)
        return Answer("ok")


def service(engine: AsyncEngine, answers: Answers) -> AskService:
    guild = FakeGuild(
        members=[FakeMember(LEO.platform_user_id), FakeMember(JOAO.platform_user_id)],
        text_channels=[FakeChannel(GENERAL, public=True)],
    )
    return AskService(
        acl=DiscordAclResolver(guild, (GENERAL,)),
        audiences=DiscordAudienceResolver(guild, (GENERAL,)),
        answers=answers,  # type: ignore[arg-type]
        limiter=RateLimiter(),
        facts=build_personal_facts(engine),
    )


async def say(asks: AskService, words: str, person: PersonRef = LEO, *, dm: bool = False) -> str:
    request = AskRequest(
        person,
        words,
        None if dm else IN_GENERAL,
        location_id=person.platform_user_id if dm else GENERAL,
    )
    outcome = await asks.ask(request)
    assert outcome.scoped is not None
    return outcome.scoped.answer.text


async def rows(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT count(*) FROM person_fact"))).scalar_one())


async def test_set_show_and_forget_through_the_ask_path(clean: AsyncEngine) -> None:
    answers = Answers()
    asks = service(clean, answers)

    assert "Leo" in await say(asks, "call me Leo")
    assert EMAIL not in await say(asks, f"my email is {EMAIL}")
    await say(asks, "reply to me in Portuguese")
    assert await rows(clean) == 3

    in_channel = await say(asks, "what do you know about me?")
    assert "Leo" in in_channel and "Portuguese" in in_channel
    assert EMAIL not in in_channel and EMAIL_CHANNEL_NOTE in in_channel
    assert EMAIL in await say(asks, "what do you know about me?", dm=True)

    await say(asks, "what was decided about the deploy?")
    match = re.search(r"<<<ASKER fence=(\w+)>>>\n(.*)\n<<<END ASKER", answers.users[-1])
    assert match is not None
    block = json.loads(match.group(2))
    assert block["preferred_name"] == "Leo"
    assert block["preferred_language"] == "Portuguese"
    assert EMAIL not in answers.users[-1]

    await say(asks, "forget my email")
    assert EMAIL not in await say(asks, "what do you know about me?", dm=True)
    assert await rows(clean) == 2

    await asks.forget(ForgetRequest(LEO, None))
    assert await rows(clean) == 0


async def test_another_member_can_neither_set_nor_read_leos_facts(clean: AsyncEngine) -> None:
    asks = service(clean, Answers())
    await say(asks, f"my email is {EMAIL}", dm=True)

    assert await say(asks, "Leo's email is other@example.com", JOAO) == FACT_ABOUT_SOMEONE_ELSE
    for dm in (False, True):
        refused = await say(asks, f"what is <@{LEO.platform_user_id}>'s email?", JOAO, dm=dm)
        assert refused == FACT_OTHERS_REFUSED
    shown_to_joao = await say(asks, "what do you know about me?", JOAO, dm=True)
    assert EMAIL not in shown_to_joao
    assert await rows(clean) == 1
