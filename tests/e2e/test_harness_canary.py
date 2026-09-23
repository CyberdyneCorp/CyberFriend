"""The harness's own contract: the internals it leans on, and the seal it relies on.

The fake Discord wire drives discord.py through private names. Each one is
listed here, so an upgrade that renames or moves one fails in this file,
naming it, rather than as an obscure error in a scenario. If one of these
fails after a discord.py upgrade, fix `harness/discord_wire.py` and bump the
pin in `pyproject.toml` together.
"""

from __future__ import annotations

import inspect
import tomllib
from contextvars import ContextVar
from pathlib import Path

import discord
import httpx
import pytest
from discord import app_commands
from discord.http import HTTPClient, Route
from discord.state import ConnectionState
from discord.ui.view import View, ViewStore
from discord.webhook import async_ as webhook_async

from chatmemory.adapters.chain.rpc import ChainReader
from chatmemory.adapters.chain.tokens import ETHEREUM
from tests.e2e.harness.conversation import E2EBot, LanguageMismatch
from tests.e2e.harness.discord_wire import (
    CommandMentionedButNotOffered,
    CommandNotOffered,
    UnexpectedDiscordCall,
)
from tests.e2e.harness.model import UnscriptedCall
from tests.e2e.harness.web import INFURA_HOSTS, FakeWeb, NetworkCanary, UnexpectedEgress

WALLET = "0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0"

ROOT = Path(__file__).resolve().parents[2]

# Every private name `harness/discord_wire.py` touches, by owner. Instance
# attributes (`Client._connection`, `CommandTree._http`) have no class
# attribute to find, so each has a source check below instead.
INTERNALS = [
    (ConnectionState, "_add_guild_from_data"),
    (ConnectionState, "_get_private_channel"),
    (ConnectionState, "add_dm_channel"),
    (discord.Client, "_async_setup_hook"),
    (discord.Guild, "_add_member"),
    (app_commands.CommandTree, "_call"),
    (app_commands.CommandTree, "on_error"),
    (webhook_async, "async_context"),
    (webhook_async.AsyncWebhookAdapter, "request"),
    (HTTPClient, "request"),
    # `FakeDiscord.press`: the view stored for a message, and its dispatch.
    (ConnectionState, "store_view"),
    (View, "_scheduled_task"),
]

# The REST routes `FakeHTTP.request` answers, and the methods that send them.
ROUTES = [
    (HTTPClient.send_message, "'/channels/{channel_id}/messages'"),
    (HTTPClient.edit_message, "'/channels/{channel_id}/messages/{message_id}'"),
    (HTTPClient.send_typing, "'/channels/{channel_id}/typing'"),
    (HTTPClient.start_private_message, "'/users/@me/channels'"),
    (HTTPClient.get_user, "'/users/{user_id}'"),
    (HTTPClient.bulk_upsert_global_commands, "'/applications/{application_id}/commands'"),
    (
        HTTPClient.bulk_upsert_guild_commands,
        "'/applications/{application_id}/guilds/{guild_id}/commands'",
    ),
]


def test_discord_py_is_the_version_the_wire_was_written_against() -> None:
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    assert "discord-py~=2.7.1" in dev["dev"]
    assert discord.__version__.startswith("2.7."), discord.__version__


@pytest.mark.parametrize(("owner", "name"), INTERNALS, ids=lambda v: getattr(v, "__name__", v))
def test_the_internals_the_wire_uses_still_exist(owner: object, name: str) -> None:
    assert hasattr(owner, name), f"discord.py no longer has {owner!r}.{name}"


def test_the_webhook_adapter_is_chosen_per_context() -> None:
    assert isinstance(webhook_async.async_context, ContextVar)


def test_the_client_keeps_its_connection_state_in_connection() -> None:
    """`FakeDiscord.state`: every guild, member, message and interaction is built on it."""
    assert "self._connection" in inspect.getsource(discord.Client.__init__)


def test_stored_views_are_kept_by_message_id_and_item_key() -> None:
    """`FakeDiscord.press` finds a button's item in `_view_store._views`."""
    assert "self._view_store: ViewStore" in inspect.getsource(ConnectionState.clear)
    assert "self._views[message_id] = dispatch_info" in inspect.getsource(ViewStore.add_view)
    assert "(item.type.value, item.custom_id)" in inspect.getsource(ViewStore.add_view)


def test_the_command_tree_keeps_its_own_http_client() -> None:
    """Why `FakeDiscord.start` replaces `tree._http` as well as `client.http`."""
    assert "self._http = client.http" in inspect.getsource(app_commands.CommandTree.__init__)


@pytest.mark.parametrize(("method", "path"), ROUTES, ids=lambda v: getattr(v, "__name__", ""))
def test_the_routes_the_wire_answers_are_the_ones_discord_py_sends(
    method: object, path: str
) -> None:
    assert path in inspect.getsource(method)  # type: ignore[arg-type]


def test_routes_still_carry_what_the_wire_reads() -> None:
    path = "/channels/{channel_id}/messages"
    route = Route("POST", path, channel_id=5)
    assert (route.method, route.path, route.channel_id) == ("POST", path, 5)
    webhook = Route("POST", "/w/{webhook_id}/{webhook_token}", webhook_id=1, webhook_token="t")
    assert (webhook.webhook_id, webhook.webhook_token) == (1, "t")
    assert Route("PUT", "/g/{guild_id}", guild_id=2).guild_id == 2
    assert Route("GET", "/users/{user_id}", user_id=3).url.endswith("/users/3")


async def test_a_real_http_client_cannot_reach_the_network() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NetworkCanary):
            await client.get("https://example.com/")


async def test_a_host_no_fixture_answers_for_is_refused() -> None:
    web = FakeWeb({})
    async with httpx.AsyncClient(transport=web.transport) as client:
        with pytest.raises(UnexpectedEgress):
            await client.get("https://example.com/")


async def test_a_provider_that_swallows_an_unscripted_host_still_fails_the_turn(
    bot: E2EBot,
) -> None:
    """The chain provider catches the refusal and answers "could not be
    reached"; the turn must fail anyway, naming the host."""
    for host in INFURA_HOSTS:
        bot.web.unscript(host)

    with pytest.raises(UnexpectedEgress, match="mainnet.infura.io"):
        await bot.dm(bot.person("Leo")).say(f"Que posicoes no Uniswap temos em {WALLET}")


async def test_a_provider_off_the_transport_fails_the_turn_it_ran_in(bot: E2EBot) -> None:
    """A real provider built without the edges' transport: it swallows the
    canary and reports the chain unreachable, and the turn still fails."""
    reader = ChainReader(ETHEREUM, "e2e-infura")
    read: list[object] = []

    async def act() -> None:
        read.append(await reader.balances(WALLET, ()))

    with pytest.raises(NetworkCanary, match="mainnet.infura.io"):
        await bot.turn(act)
    assert read and getattr(read[0], "unreachable", None), "the provider did not swallow it"


@pytest.mark.parametrize(
    "violation",
    [
        CommandMentionedButNotOffered,
        CommandNotOffered,
        UnexpectedDiscordCall,
        UnexpectedEgress,
        NetworkCanary,
        UnscriptedCall,
    ],
    ids=lambda v: v.__name__,
)
def test_a_harness_violation_cannot_satisfy_a_known_open_language_xfail(
    violation: type[Exception],
) -> None:
    """The open-item xfails expect `LanguageMismatch` only; a violation that
    were one would be absorbed as the expected failure."""
    assert not issubclass(violation, LanguageMismatch)
