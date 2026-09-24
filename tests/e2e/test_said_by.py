""""What did Leo say about the deploy last week", through the assembled bot.

Seeded: Leo and Bea in one #general conversation last week, Leo in #general
just outside last week on both sides, Leo and Lia in #leadership (which only
leads read), two Joãos in #general and a third only in #leadership.

The harness clock stands at Tuesday 1 September 2026, 12:00 UTC -- 09:00 in
Sao Paulo -- so "semana passada" is Monday 24 August to Monday 31 August,
local midnights. Leo's lines at 23:30 on the Sunday and 00:30 on the Monday
are both on the same UTC day: only a calendar week in Sao Paulo tells them
apart. The scripted model quotes whatever evidence it is shown, so a line
that reached the prompt would be in the reply.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import discord

from chatmemory.domain.identity import PersonRef
from tests.e2e.harness.conversation import PLATFORM, E2EBot, Turn
from tests.e2e.harness.process import GENERAL, GUILD_ID, LEAD

SAO_PAULO = timezone(timedelta(hours=-3))

IN_WEEK = "the deploy is scheduled for friday night"
BEA_BESIDE_LEO = "the deploy checklist still misses the rollback"
THIS_WEEK = "deploy freeze starts monday morning"
WEEK_BEFORE = "the deploy drill happened on sunday"
LEO_PRIVATE = "the deploy will slip because of the reorg"
LIA_PRIVATE = "the layoffs plan ships with the deploy"

OUTSIDE = (BEA_BESIDE_LEO, THIS_WEEK, WEEK_BEFORE, LEO_PRIVATE, LIA_PRIVATE)


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in Sao Paulo in August 2026."""
    return datetime(2026, 8, day, hour, minute, tzinfo=SAO_PAULO)


def ref(member: discord.Member) -> PersonRef:
    return PersonRef(PLATFORM, member.id)


async def seed(bot: E2EBot) -> dict[str, discord.Member]:
    people = {
        "leo": bot.person("Leo"),
        "bea": bot.person("Bea"),
        "lia": bot.person("Lia", roles=[LEAD]),
    }
    leo, bea, lia = ref(people["leo"]), ref(people["bea"]), ref(people["lia"])
    await bot.seed_conversation(
        "general",
        [
            (leo, "Leo", IN_WEEK, local(30, 23, 30)),
            (bea, "Bea", BEA_BESIDE_LEO, local(30, 23, 31)),
        ],
    )
    await bot.seed_conversation("general", [(leo, "Leo", THIS_WEEK, local(31, 0, 30))])
    await bot.seed_conversation("general", [(leo, "Leo", WEEK_BEFORE, local(23, 23, 30))])
    await bot.seed_conversation("leadership", [(leo, "Leo", LEO_PRIVATE, local(27, 10))])
    await bot.seed_conversation("leadership", [(lia, "Lia", LIA_PRIVATE, local(27, 11))])
    for uid, name, channel in (
        (900_101, "João Silva", "general"),
        (900_102, "João Pereira", "general"),
        (900_103, "João Costa", "leadership"),
    ):
        await bot.seed_conversation(
            channel, [(PersonRef(PLATFORM, uid), name, f"{name} says hello", local(26, 9))]
        )
    return people


def model_saw(bot: E2EBot, text: str) -> bool:
    return any(text in prompt for _, prompt in bot.chat.calls)


def assert_only_leo_in_week(bot: E2EBot, turn: Turn) -> None:
    assert IN_WEEK in turn.text
    link = f"https://discord.com/channels/{GUILD_ID}/{GENERAL}/{bot.corpus[IN_WEEK]}"
    assert link in turn.text, "the citation lands on Leo's own message"
    for line in OUTSIDE:
        assert line not in turn.text, f"{line!r} reached the reply"
        assert not model_saw(bot, line), f"{line!r} reached the model"


async def test_what_leo_said_last_week_is_his_readable_lines_only(bot: E2EBot) -> None:
    people = await seed(bot)

    turn = await bot.dm(people["bea"]).say("o que o Leo disse sobre o deploy semana passada?")

    assert turn.edge() == "CORPUS"
    assert_only_leo_in_week(bot, turn)
    assert "-# O que Leo disse sobre o deploy de 24/08 a 30/08:" in turn.text

    english = await bot.dm(people["bea"]).say("what did Leo say about the deploy last week?")
    assert_only_leo_in_week(bot, english)
    assert "-# What Leo said about the deploy from 24 Aug to 30 Aug:" in english.text


async def test_the_asker_and_a_mention_resolve_without_a_name(bot: E2EBot) -> None:
    people = await seed(bot)

    mine = await bot.dm(people["leo"]).say("o que eu falei sobre o deploy semana passada?")
    assert_only_leo_in_week(bot, mine)
    assert "-# O que você disse" in mine.text

    mentioned = await bot.dm(people["bea"]).say(
        f"<@{people['leo'].id}> comentou algo sobre o deploy semana passada?"
    )
    assert_only_leo_in_week(bot, mentioned)


async def test_a_lead_asking_in_general_is_answered_for_the_room(bot: E2EBot) -> None:
    """Lia may read #leadership; #general may not, so neither may the answer."""
    people = await seed(bot)

    turn = await bot.channel("general", people["lia"]).say(
        "o que o Leo disse sobre o deploy semana passada?"
    )

    assert_only_leo_in_week(bot, turn)
    assert not [s for s in turn.sent if s.via == "dm"], "no 'there is more' notice"


async def test_an_ambiguous_name_asks_which_naming_only_visible_people(bot: E2EBot) -> None:
    people = await seed(bot)

    turn = await bot.dm(people["bea"]).say("o que o João disse?")

    assert "Qual João?" in turn.text
    assert "João Silva" in turn.text and "João Pereira" in turn.text
    assert "João Costa" not in turn.text, "only speaks in a channel Bea cannot read"
    assert not turn.searched and turn.schemas == (), "no retrieval and no model call"


async def test_a_person_only_in_a_private_channel_is_not_revealed(bot: E2EBot) -> None:
    people = await seed(bot)

    turn = await bot.dm(people["bea"]).say("what did Lia say about the layoffs plan?")

    assert LIA_PRIVATE not in turn.text
    assert not model_saw(bot, LIA_PRIVATE)
    assert "Lia" not in turn.text.replace("what did Lia say", "")

    control = await bot.dm(people["lia"]).say("what did Lia say about the layoffs plan?")
    assert LIA_PRIVATE in control.text
