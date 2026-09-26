"""Commands about the person themselves are offered in a DM with the bot.

Regression, from production: in a DM with the bot, typing "/" listed no
CyberFriend command at all -- every command was registered on the guild, and
Discord never lists guild commands in a DM. "/forget" typed as text was then
answered "type / and pick /forget", which could not be done.

Driven through the real `setup_hook`, with only the network sync replaced.
"""

from __future__ import annotations

import discord
import pytest

from chatmemory.adapters.discord.bot import CyberFriendClient

GUILD_ID = 1
PERSONAL = {
    "ask", "resolve", "forget", "notifications", "channels", "schedule", "alert",
    "suggest", "suggestions", "privacy",
}
CHANNEL_ACTIONS = {"index", "unindex"}


@pytest.fixture
async def registered(monkeypatch: pytest.MonkeyPatch) -> CyberFriendClient:
    client = CyberFriendClient(object(), GUILD_ID)  # type: ignore[arg-type]
    synced: list[object] = []

    async def sync(*, guild: object = None) -> list[object]:
        synced.append(guild)
        return []

    monkeypatch.setattr(client.tree, "sync", sync)
    await client.setup_hook()
    # Both scopes are synced: global for the new home of the personal
    # commands, and the guild so their old guild copies are removed.
    assert None in synced and any(g is not None for g in synced)
    return client


async def test_personal_commands_are_global_and_allowed_in_a_dm(
    registered: CyberFriendClient,
) -> None:
    global_commands = {c.name: c for c in registered.tree.get_commands()}
    assert set(global_commands) == PERSONAL
    for name, command in global_commands.items():
        contexts = command.allowed_contexts
        assert contexts is not None and contexts.dm_channel, f"/{name} is hidden in DMs"
        assert contexts.guild, f"/{name} is missing from the server"
        installs = command.allowed_installs
        assert installs is not None and not installs.user, f"/{name} is user-installable"


async def test_channel_commands_stay_in_the_server_only(registered: CyberFriendClient) -> None:
    in_guild = {c.name for c in registered.tree.get_commands(guild=discord.Object(GUILD_ID))}
    assert in_guild == CHANNEL_ACTIONS
