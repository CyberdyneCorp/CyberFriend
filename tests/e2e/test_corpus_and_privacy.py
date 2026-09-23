"""The corpus path, and the one rule it must never break.

The control: an ordinary question in a channel searches the archive and cites
what it found, under the source heading. It is what every routed scenario is
the exception to, so it is proved to still work through the same wire.

The rule: a colleague's message in a channel the asker cannot read never
reaches the answer -- nor the model that writes it. The scripted model quotes
whatever evidence it is shown, so a window that leaked into the prompt would
be in the reply. The positive control is the same question from somebody who
can read the channel.
"""

from __future__ import annotations

from tests.e2e.conftest import COFFEE, COLLEAGUE_CRYPTO, LEADERSHIP_SECRET
from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.process import GENERAL, LEAD

LAYOFFS = "what is the layoffs plan for the reorganisation?"


def _model_saw(bot: E2EBot, text: str) -> bool:
    return any(text in prompt for _, prompt in bot.chat.calls)


async def test_a_channel_question_is_answered_from_the_archive_and_cited(bot: E2EBot) -> None:
    turn = await bot.channel("general", bot.person("Bea")).say(
        "when will the coffee maker be fixed?"
    )

    assert turn.edge() == "CORPUS"
    [reply] = turn.sent
    assert reply.via == "reply" and reply.channel_id == GENERAL
    assert COFFEE in reply.content
    assert "-# **From this server:**" in reply.content
    assert f"https://discord.com/channels/7/{GENERAL}" in reply.content
    assert COLLEAGUE_CRYPTO not in reply.content
    turn.assert_language("en")


async def test_a_channel_the_asker_cannot_read_never_reaches_the_answer(bot: E2EBot) -> None:
    turn = await bot.dm(bot.person("Bea")).say(LAYOFFS)

    assert turn.searched
    assert LEADERSHIP_SECRET not in turn.text
    assert not _model_saw(bot, LEADERSHIP_SECRET), "the model was shown a channel Bea cannot read"


async def test_a_channel_answer_leaves_out_what_the_room_cannot_read(bot: E2EBot) -> None:
    """The asker may read #leadership; #general may not. The answer posted
    there is scoped to the room, and the asker is told privately there is more."""
    lead = bot.person("Lia", roles=[LEAD])
    bot.person("Bea")

    turn = await bot.channel("general", lead).say(LAYOFFS)

    assert LEADERSHIP_SECRET not in turn.text
    assert not _model_saw(bot, LEADERSHIP_SECRET)
    in_channel = [s for s in turn.sent if s.channel_id == GENERAL]
    assert in_channel, "nothing was posted in the channel"
    notices = [s for s in turn.sent if s.via == "dm"]
    assert [n.channel_id for n in notices] == [bot.discord.dm_channel_id(lead.id)]


async def test_somebody_who_can_read_the_channel_is_answered_from_it(bot: E2EBot) -> None:
    turn = await bot.dm(bot.person("Lia", roles=[LEAD])).say(LAYOFFS)

    assert turn.edge() == "CORPUS"
    assert LEADERSHIP_SECRET in turn.text
