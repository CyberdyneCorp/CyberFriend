"""The fixture API: people, conversations and turns over the assembled process.

A `Turn` is what one message or command produced, observed only at the edges:
what Discord received, whether the corpus was searched, which hosts were
reached and which model stages ran. Memory and facts are read back by SQL from
the real tables, never from the service under test. None of that depends on
how the bot decides a route internally, so these scenarios keep passing
unchanged when the routing is rewritten -- and fail when its effect changes.

After every turn the harness drains background summaries, fails if any request
was refused -- a host no fixture answers for, or a real connection -- even when
the provider that made it swallowed the error, and checks that each `/name` the
bot said is a command Discord offers where it said it.
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

from chatmemory.adapters.discord import alerts as discord_alerts
from chatmemory.adapters.discord import bot as discord_bot
from chatmemory.adapters.store.postgres import PostgresStore
from chatmemory.app import alert_requests, ask, catchup, localise
from chatmemory.app.asks import obligations
from chatmemory.app.ingest import EmbeddingWorker
from chatmemory.app.language import Language, detect
from chatmemory.app.reasoning import contract, loop, service
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.domain.messages import Message, Window
from chatmemory.entrypoints.bot import Process
from tests.e2e.harness.discord_wire import (
    CommandMentionedButNotOffered,
    FakeDiscord,
    Sent,
    snowflake,
)
from tests.e2e.harness.ingest import Ingest
from tests.e2e.harness.model import HashEmbeddings, ScriptedChat
from tests.e2e.harness.web import (
    BLOCKSCOUT_HOSTS,
    INFURA_HOSTS,
    PRICE_HOSTS,
    FakeWeb,
    NetworkCanary,
    NetworkSeal,
    UnexpectedEgress,
)

Edge = Literal["CORPUS", "CHAIN", "MARKET", "WEB", "NONE"]

PLATFORM = discord_bot.PLATFORM
COLLEAGUE = PersonRef(PLATFORM, 800_001)
"""Who wrote the seeded corpus: somebody who is not a member in any scenario."""

CHAIN_HOSTS = frozenset(INFURA_HOSTS + BLOCKSCOUT_HOSTS)
MARKET_HOSTS = frozenset({*PRICE_HOSTS, "api.frankfurter.dev"})
WEB_HOSTS = frozenset({"en.wikipedia.org", "pt.wikipedia.org", "serpapi.com"})

_COMMAND_MENTION = re.compile(r"(?:^|[\s`(*])/([a-z][a-z0-9_-]{1,31})\b")

# Modules whose module-level English strings a Portuguese turn must never
# contain. Collected rather than listed, so a new English constant in one of
# them is covered without editing a test.
_FIXED_REPLY_MODULES: tuple[ModuleType, ...] = (
    discord_bot,
    discord_alerts,
    alert_requests,
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


class LanguageMismatch(AssertionError):
    """A turn said something in the other language.

    Its own type, so a known-open language defect can be marked as expecting
    exactly this -- and a harness violation in the same scenario still fails.
    """


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
            if detect(line) is other:
                raise LanguageMismatch(f"{other} line in a {lang} turn: {line!r}")
        if lang == "pt":
            leaked = sorted(s for s in ENGLISH_FIXED_REPLIES if s in self.text)
            if leaked:
                raise LanguageMismatch(f"fixed English reply in a Portuguese turn: {leaked}")


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

    async def slash(self, name: str, *, locale: str = "en-US", **options: object) -> Turn:
        """Run a slash command here. Raises `CommandNotOffered` where Discord
        would not list it. `locale` is the person's Discord client language."""
        wire = self._bot.discord
        interaction = wire.interaction(
            self.who, name, options, channel=self.channel, locale=locale
        )

        async def run() -> None:
            with wire.webhooks_installed():
                await wire.client.tree._call(interaction)

        return await self._bot.turn(run)

    async def press(self, sent: Sent, label: str) -> Turn:
        """This person presses the button `label` on a message the bot sent."""
        wire = self._bot.discord

        async def run() -> None:
            with wire.webhooks_installed():
                await wire.press(self.who, sent, label)

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
        seal: NetworkSeal,
        ingest: Ingest,
    ) -> None:
        self.process = process
        self.ingest = ingest
        self.seal = seal
        self.discord = discord
        self.chat = chat
        self.web = web
        self.embeddings = embeddings
        self.engine: AsyncEngine = process.stack.engine
        self.corpus: dict[str, int] = {}
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
        refused, sealed = len(self.web.refused), len(self.seal.refused)
        await act()
        await self.process.conversations.drain()
        self._check_nothing_refused(self.web.refused[refused:], self.seal.refused[sealed:])
        turn = Turn(
            sent=tuple(self.discord.http.sent[sent:]),
            searched=self._search.calls > searches,
            hosts=self.web.hosts(hosts),
            schemas=tuple(schema for schema, _ in self.chat.calls[calls:]),
        )
        self._check_commands_offered(turn)
        return turn

    @staticmethod
    def _check_nothing_refused(unscripted: Sequence[str], real: Sequence[str]) -> None:
        """Providers answer "could not be reached" rather than raise, so a
        refused request is failed here, after the fact, naming the host."""
        if real:
            raise NetworkCanary(f"real network call to {', '.join(sorted(set(real)))}")
        if unscripted:
            raise UnexpectedEgress(f"no fixture for {', '.join(sorted(set(unscripted)))}")

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

    async def chatter(
        self,
        name: str,
        who: discord.Member,
        content: str,
        *,
        mentions: Sequence[discord.Member] = (),
        at: datetime,
    ) -> int:
        """`who` says `content` in #`name`, not to the bot, and ingest captures it.

        Returns the message id, and adds the text to `corpus`. Nothing is
        extracted until `extract_asks`, as nothing is live until a flush.
        """
        raw = self.discord.chatter(
            who, self.discord.channel(name), content, mentions=mentions, at=at
        )
        message = await self.ingest.capture(raw)
        self.corpus[content] = message.platform_message_id
        return message.platform_message_id

    async def extract_asks(self) -> tuple[str, ...]:
        """Run the ingest extraction pass; returns the model stages it called."""
        calls = len(self.chat.calls)
        await self.ingest.extract()
        return tuple(schema for schema, _ in self.chat.calls[calls:])

    async def seed_corpus(self, windows: Sequence[tuple[str, str, datetime]]) -> None:
        """Archived messages, each its own window, written the way ingest writes them.

        Through the production store and embedding worker, so a window carries
        its message ids and a citation links to the message. `corpus` maps
        each text to its message id.
        """
        store = PostgresStore(self.engine)
        for name, body, at in windows:
            channel = ChannelRef(PLATFORM, self.discord.channel(name).id)
            message_id = snowflake()
            await store.upsert_messages(
                [Message(message_id, channel, COLLEAGUE, body, at, author_display="Colleague")]
            )
            await store.replace_windows(channel, [Window(channel, (message_id,), body, at, at)])
            self.corpus[body] = message_id
        await EmbeddingWorker(store, self.embeddings, batch_size=len(windows) or 1).run_once()

    async def seed_conversation(
        self, name: str, lines: Sequence[tuple[PersonRef, str, str, datetime]]
    ) -> list[int]:
        """One archived window of several people's (author, name, body, at) lines.

        Rendered as windowing renders it, so the window text holds every
        author's words -- which is what an author-scoped answer must not show.
        Returns the message ids, in order, and adds each body to `corpus`.
        """
        store = PostgresStore(self.engine)
        channel = ChannelRef(PLATFORM, self.discord.channel(name).id)
        messages = [
            Message(snowflake(), channel, who, body, at, author_display=display)
            for who, display, body, at in lines
        ]
        await store.upsert_messages(messages)
        rendered = "\n".join(f"{m.author_display}: {m.content}" for m in messages)
        ids = tuple(m.platform_message_id for m in messages)
        await store.replace_windows(
            channel,
            [Window(channel, ids, rendered, messages[0].created_at, messages[-1].created_at)],
        )
        self.corpus.update({m.content: m.platform_message_id for m in messages})
        await EmbeddingWorker(store, self.embeddings, batch_size=8).run_once()
        return list(ids)

    async def facts_of(self, who: discord.Member) -> dict[str, str]:
        """The person's stored facts, by SQL: one value per kind (for a wallet
        kind with several, any one of them -- use `fact_rows`)."""
        return dict(await self.fact_rows(who))

    async def fact_rows(self, who: discord.Member) -> list[tuple[str, str]]:
        """Every stored (kind, value), in the order saved, by SQL."""
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT f.kind, f.value FROM person_fact f "
                    "JOIN person_platform_id p ON p.person_id = f.person_id "
                    "WHERE p.platform = 'discord' AND p.platform_user_id = :user "
                    "ORDER BY f.id"
                ),
                {"user": who.id},
            )
            return [(str(kind), str(value)) for kind, value in rows]
