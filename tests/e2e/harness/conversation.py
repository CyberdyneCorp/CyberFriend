"""The fixture API: people, conversations and turns over the assembled process.

A `Turn` is what one message or command produced, observed only at the edges:
what Discord received, whether the corpus was searched, which hosts were
reached and which model stages ran. Memory and facts are read back by SQL from
the real tables, never from the service under test. None of that depends on
how the bot decides a route internally, so these scenarios keep passing
unchanged when the routing is rewritten -- and fail when its effect changes.

After every turn the harness drains background summaries and checks that each
`/name` the bot said is a command Discord offers where it said it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import Any, Literal

import discord
from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine

from chatmemory.adapters.discord import bot as discord_bot
from chatmemory.app import ask, catchup, localise
from chatmemory.app.asks import obligations
from chatmemory.app.language import Language, detect
from chatmemory.app.reasoning import contract, loop, service
from chatmemory.entrypoints.bot import Process
from tests.e2e.harness.discord_wire import CommandMentionedButNotOffered, FakeDiscord, Sent
from tests.e2e.harness.model import HashEmbeddings, ScriptedChat
from tests.e2e.harness.web import BLOCKSCOUT_HOSTS, INFURA_HOSTS, PRICE_HOSTS, FakeWeb

Edge = Literal["CORPUS", "CHAIN", "MARKET", "WEB", "NONE"]

CHAIN_HOSTS = frozenset(INFURA_HOSTS + BLOCKSCOUT_HOSTS)
MARKET_HOSTS = frozenset({*PRICE_HOSTS, "api.frankfurter.dev"})
WEB_HOSTS = frozenset({"en.wikipedia.org", "pt.wikipedia.org", "serpapi.com"})

_COMMAND_MENTION = re.compile(r"(?:^|[\s`(*])/([a-z][a-z0-9_-]{1,31})\b")

# Modules whose module-level English strings a Portuguese turn must never
# contain. Collected rather than listed, so a new English constant in one of
# them is covered without editing a test.
_FIXED_REPLY_MODULES: tuple[ModuleType, ...] = (
    discord_bot,
    ask,
    catchup,
    obligations,
    contract,
    loop,
    service,
    localise,
)

_INLINE_ENGLISH = ("You've hit your question limit",)
"""English written inline rather than as a constant: the rate-limit reply."""


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _module_constants(module: ModuleType) -> Iterable[str]:
    for name, value in vars(module).items():
        if name.isupper() or name == "PORTUGUESE":
            yield from _strings(value)
    yield from getattr(module, "PORTUGUESE", {})


def english_fixed_replies() -> frozenset[str]:
    """Every fixed English reply the answer paths and the adapter can send."""
    found = {
        s.strip()
        for module in _FIXED_REPLY_MODULES
        for s in _module_constants(module)
        if "{" not in s and len(s) > 12 and detect(s) is Language.ENGLISH
    }
    return frozenset(found | set(_INLINE_ENGLISH))


ENGLISH_FIXED_REPLIES = english_fixed_replies()


@dataclass(frozen=True)
class Turn:
    """What one message or command produced, seen from outside the process."""

    sent: tuple[Sent, ...]
    searched: bool
    hosts: frozenset[str]
    schemas: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(s.content for s in self.sent)

    def edge(self) -> Edge:
        """Where the answer came from, judged by which edge was touched."""
        if self.hosts & CHAIN_HOSTS:
            return "CHAIN"
        if self.hosts & MARKET_HOSTS:
            return "MARKET"
        if self.hosts & WEB_HOSTS:
            return "WEB"
        return "CORPUS" if self.searched else "NONE"

    def assert_language(self, lang: Literal["pt", "en"]) -> None:
        """Every line said is not in the other language, and a PT turn quotes
        none of the fixed English replies."""
        other = Language.ENGLISH if lang == "pt" else Language.PORTUGUESE
        for line in self.text.splitlines():
            if line.startswith("-#") or len(line.split()) < 4:
                continue
            assert detect(line) is not other, f"{other} line in a {lang} turn: {line!r}"
        if lang == "pt":
            leaked = sorted(s for s in ENGLISH_FIXED_REPLIES if s in self.text)
            assert not leaked, f"fixed English reply in a Portuguese turn: {leaked}"


class _SearchSpy:
    """Counts corpus searches on the real backend, and delegates every one."""

    def __init__(self, search: Any) -> None:
        self.calls = 0
        self._search = search.search

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return await self._search(*args, **kwargs)


class Conversation:
    """One person talking to the bot in one place: their DM, or a channel."""

    def __init__(
        self, bot: E2EBot, who: discord.Member, channel: discord.TextChannel | None
    ) -> None:
        self._bot = bot
        self.who = who
        self.channel = channel

    @property
    def location_id(self) -> int:
        """Where memory files this conversation: the channel it happens in."""
        if self.channel is not None:
            return self.channel.id
        return self._bot.discord.dm_channel_id(self.who.id)

    async def say(self, content: str) -> Turn:
        """A DM, or a channel message with the bot mentioned."""
        wire = self._bot.discord
        if self.channel is None:
            message = wire.dm_message(self.who, content)
        else:
            message = wire.channel_message(self.who, self.channel, content)
        return await self._bot.turn(lambda: wire.client.on_message(message))

    async def mention_only(self) -> Turn:
        return await self.say("")

    async def slash(self, name: str, **options: object) -> Turn:
        """Run a slash command here. Raises `CommandNotOffered` where Discord
        would not list it."""
        wire = self._bot.discord
        interaction = wire.interaction(self.who, name, options, channel=self.channel)

        async def run() -> None:
            with wire.webhooks_installed():
                await wire.client.tree._call(interaction)

        return await self._bot.turn(run)

    async def memory_turns(self) -> list[Row[Any]]:
        """The remembered turns of this conversation, by SQL."""
        async with self._bot.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT t.question, t.answer, t.channel_ids, t.location_direct "
                    "FROM conversation_turn t "
                    "JOIN person_platform_id p ON p.person_id = t.person_id "
                    "WHERE p.platform = 'discord' AND p.platform_user_id = :user "
                    "AND t.location_id = :location ORDER BY t.id"
                ),
                {"user": self.who.id, "location": self.location_id},
            )
            return list(rows)


class E2EBot:
    """The assembled bot process, its fake edges, and the people in its guild."""

    def __init__(
        self,
        process: Process,
        discord: FakeDiscord,
        chat: ScriptedChat,
        web: FakeWeb,
        embeddings: HashEmbeddings,
    ) -> None:
        self.process = process
        self.discord = discord
        self.chat = chat
        self.web = web
        self.embeddings = embeddings
        self.engine: AsyncEngine = process.stack.engine
        self._search = _SearchSpy(process.stack.search)
        # An instance attribute shadows the method for this object only: the
        # real search still runs, and every other instance is untouched.
        process.stack.search.search = self._search  # type: ignore[method-assign]

    def person(self, name: str, *, roles: Sequence[str] = ()) -> discord.Member:
        return self.discord.add_member(name, roles=roles)

    def dm(self, who: discord.Member) -> Conversation:
        return Conversation(self, who, None)

    def channel(self, name: str, who: discord.Member) -> Conversation:
        return Conversation(self, who, self.discord.channel(name))

    async def turn(self, act: Callable[[], Any]) -> Turn:
        """Run one inbound event to completion and report what it touched."""
        sent, hosts, calls = len(self.discord.http.sent), len(self.web.calls), len(self.chat.calls)
        searches = self._search.calls
        await act()
        await self.process.conversations.drain()
        turn = Turn(
            sent=tuple(self.discord.http.sent[sent:]),
            searched=self._search.calls > searches,
            hosts=self.web.hosts(hosts),
            schemas=tuple(schema for schema, _ in self.chat.calls[calls:]),
        )
        self._check_commands_offered(turn)
        return turn

    def _check_commands_offered(self, turn: Turn) -> None:
        for sent in turn.sent:
            dm = self.discord.is_dm(sent.channel_id)
            offered = self.discord.offered(dm=dm)
            for name in _COMMAND_MENTION.findall(sent.content):
                if name not in offered:
                    where = "a DM" if dm else "the guild"
                    raise CommandMentionedButNotOffered(
                        f"the bot said /{name} in {where}, where Discord does not offer it: "
                        f"{sent.content!r}"
                    )

    async def seed_corpus(self, windows: Sequence[tuple[str, str, datetime]]) -> None:
        """Archived conversation windows, inserted the way ingest leaves them."""
        async with self.engine.begin() as conn:
            for spec in self.discord.layout.channels:
                await conn.execute(
                    text(
                        "INSERT INTO channel (id, platform, name, is_indexed) "
                        "VALUES (:id, 'discord', :n, TRUE) ON CONFLICT DO NOTHING"
                    ),
                    {"id": spec.id, "n": spec.name},
                )
            for name, body, at in windows:
                await conn.execute(
                    text(
                        "INSERT INTO conversation_window "
                        "(channel_id, text, starts_at, ends_at, search_tsv, embedding) "
                        "VALUES (:c, :t, :s, :s, to_tsvector('english', :t), "
                        "CAST(:e AS vector))"
                    ),
                    {
                        "c": self.discord.channel(name).id,
                        "t": body,
                        "s": at,
                        "e": "[" + ",".join(f"{v:.6f}" for v in self.embeddings.vector(body)) + "]",
                    },
                )

    async def facts_of(self, who: discord.Member) -> dict[str, str]:
        """The person's stored facts, by SQL."""
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT f.kind, f.value FROM person_fact f "
                    "JOIN person_platform_id p ON p.person_id = f.person_id "
                    "WHERE p.platform = 'discord' AND p.platform_user_id = :user"
                ),
                {"user": who.id},
            )
            return {kind: value for kind, value in rows}
