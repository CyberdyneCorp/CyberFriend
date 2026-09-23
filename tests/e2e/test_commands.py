"""S7 and S8: the personal commands are offered in a DM, as Discord sees them.

The production failure: `/forget` and the other personal commands were
registered on the guild only, and Discord never lists guild commands in a DM --
so a person told to "pick /forget from the menu" in a DM had no such entry. The
unit tests read in-memory tree attributes with `sync` stubbed out, and nothing
modelled what Discord would offer where.

Here the real `setup_hook` syncs to the fake wire, and what is asserted is the
payload Discord would receive. Every `/name` the bot says is checked against
what is offered where it said it (in `E2EBot.turn`), so a reply pointing at a
command that is not there fails whichever scenario produced it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from chatmemory.app.self_description import ALWAYS_AVAILABLE, NOTIFICATIONS, SCHEDULED
from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.discord_wire import CommandNotOffered
from tests.e2e.harness.process import GUILD_ID

SNAPSHOT = Path(__file__).parent / "snapshots" / "commands.json"
PERSONAL = {"ask", "resolve", "forget", "notifications", "channels", "schedule"}
GUILD_ONLY = {"index", "unindex"}
_EVERY_COMMAND = (*ALWAYS_AVAILABLE, NOTIFICATIONS, *SCHEDULED)
_LISTED = re.compile(r"`/([a-z]+)")


def _registration(bot: E2EBot) -> dict[str, Any]:
    http = bot.discord.http
    return {"global": http.global_commands, "guild": http.guild_commands.get(GUILD_ID, [])}


def test_personal_commands_are_global_and_offered_in_a_dm(bot: E2EBot) -> None:
    by_name = {c["name"]: c for c in bot.discord.http.global_commands}

    assert set(by_name) == PERSONAL
    for name, command in by_name.items():
        assert command["contexts"] == [0, 1], f"/{name} contexts"
        assert command["integration_types"] == [0], f"/{name} integration types"
    assert bot.discord.offered(dm=True) == PERSONAL


def test_channel_commands_are_offered_in_the_guild_only(bot: E2EBot) -> None:
    guild = {c["name"] for c in bot.discord.http.guild_commands[GUILD_ID]}

    assert guild == GUILD_ONLY
    assert bot.discord.offered(dm=False) == PERSONAL | GUILD_ONLY
    assert not GUILD_ONLY & bot.discord.offered(dm=True)


async def test_a_guild_only_command_cannot_be_run_from_a_dm(bot: E2EBot) -> None:
    dm = bot.dm(bot.person("Leo"))
    with pytest.raises(CommandNotOffered):
        await dm.slash("index")


def test_the_registration_matches_the_snapshot(bot: E2EBot) -> None:
    """The exact payload, so a change to what Discord is sent is a reviewed diff.

    `E2E_UPDATE_SNAPSHOTS=1` rewrites the file; the diff is the review.
    """
    registration = _registration(bot)
    if os.environ.get("E2E_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.write_text(json.dumps(registration, indent=2, sort_keys=True) + "\n")
    assert registration == json.loads(SNAPSHOT.read_text())


@pytest.mark.parametrize("where", ["dm", "general"])
async def test_every_command_self_description_advertises_is_offered(
    bot: E2EBot, where: str
) -> None:
    """Asked in a DM, the reply named `/index` and `/unindex`, which Discord
    lists only in the server. `E2EBot.turn` fails on that; this also pins that
    the reply lists commands at all, so the check has something to check."""
    ana = bot.person("Ana")
    talk = bot.dm(ana) if where == "dm" else bot.channel(where, ana)

    turn = await talk.say("what can you do?")

    advertised = set(_LISTED.findall(turn.text))
    assert not turn.searched
    assert {"ask", "forget"} <= advertised
    assert advertised <= bot.discord.offered(dm=where == "dm")


def test_the_guild_only_commands_described_are_the_ones_synced_to_the_guild(
    bot: E2EBot,
) -> None:
    guild = {c["name"] for c in bot.discord.http.guild_commands[GUILD_ID]}
    assert {c.name for c in _EVERY_COMMAND if c.guild_only} == guild


async def test_forget_typed_in_a_dm_points_at_a_command_that_is_there(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    await dm.say("call me Leo")
    await dm.say("when will the coffee maker be fixed?")
    assert await bot.facts_of(leo) == {"preferred_name": "Leo"}
    assert len(await dm.memory_turns()) == 1

    typed = await dm.say("/forget")

    assert typed.edge() == "NONE"
    assert not typed.searched
    assert "/forget" in typed.text

    # Raises CommandNotOffered if /forget were guild-only.
    forgot = await dm.slash("forget", scope="everywhere")

    [reply] = forgot.sent
    assert reply.via == "followup" and reply.ephemeral
    kinds = [kind for kind, _ in bot.discord.webhooks.events]
    assert kinds == ["response", "followup"], "the command must defer before following up"
    assert await dm.memory_turns() == []
    assert await bot.facts_of(leo) == {}
