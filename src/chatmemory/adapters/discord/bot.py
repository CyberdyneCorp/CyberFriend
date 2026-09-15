"""The Discord surface.

Identity is free here: Discord authenticates the author of every message, so
there is no client-supplied viewer parameter to forge. Content claiming to
come from someone else is disregarded -- only the authenticated author counts.

This is also the last place provenance can be shown to the person who has to
act on the answer. Once the agent can reach the internet, a reply may rest on
two very different kinds of evidence, and "what did we decide" answered by
silently blending a colleague's message with a search result is worse than no
answer at all: it reads as team knowledge and is not. So citations are
grouped and labelled by source, always -- including when every source is this
server, because a reader should never have to infer the common case from the
absence of a warning.
"""

from __future__ import annotations

from itertools import zip_longest
from typing import Any

import discord
import structlog
from discord import app_commands

from chatmemory.app.ask import AskRequest, AskService
from chatmemory.app.disclosure import ScopedAnswer, withheld_notice
from chatmemory.app.reasoning.evidence import SOURCE_DISCORD, SOURCE_WEB, SourcedCitation
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.ports.answers import Citation

log = structlog.get_logger()

PLATFORM = "discord"
MAX_REPLY_CHARS = 1900  # Discord's limit is 2000; leave room for citations.
MAX_CITATIONS = 5
MAX_EXCERPT_CHARS = 140

SOURCE_HEADINGS = {
    SOURCE_DISCORD: "**From this server:**",
    SOURCE_WEB: "**From the web:**",
}

SOURCE_FALLBACK_LABELS = {
    SOURCE_DISCORD: "jump to message",
    SOURCE_WEB: "open result",
}

CAPABILITIES = (
    "I answer questions about what's been said in the channels you can read.\n"
    "Try: `what did people ask me today?`, `what happened in #infra this week?`\n"
    "Mention me with a question, use `/ask`, or send me a direct message.\n\n"
    "Answers posted in a channel only use sources everyone here can read. "
    "Ask me in a DM to search everything *you* can read."
)


def _person(user: discord.User | discord.Member) -> PersonRef:
    return PersonRef(PLATFORM, user.id)


def _source_of(citation: Citation) -> str:
    """Where a citation came from.

    `Citation` is the port and describes a place, not a kind; evidence that
    came from outside the corpus attaches the kind on the way through. A
    plain `Citation` is the corpus, which is what every caller that predates
    egress produces.
    """
    return citation.source_system if isinstance(citation, SourcedCitation) else SOURCE_DISCORD


def _citation_line(number: int, citation: Citation) -> str:
    source = _source_of(citation)
    # Never an empty label: markdown renders `[]( url )` as a bare URL, and a
    # window spans several people, so naming one author is wrong even when a
    # name is known.
    label = citation.author_display.strip() or SOURCE_FALLBACK_LABELS.get(source, source)
    excerpt = " ".join(citation.excerpt.split())[:MAX_EXCERPT_CHARS]
    # The heading above already says where this came from; the inline tag
    # repeats it for every non-corpus line, because a line quoted, screenshot
    # or read on its own loses the heading and keeps the claim.
    tag = "" if source == SOURCE_DISCORD else f"({source}) "
    return f"{number}. {tag}[{label}]({citation.url}) — {excerpt}"


def _grouped(citations: tuple[Citation, ...]) -> list[tuple[str, list[Citation]]]:
    """Citations by source, corpus first, each group in its original order."""
    groups: dict[str, list[Citation]] = {}
    for citation in citations:
        groups.setdefault(_source_of(citation), []).append(citation)
    corpus = [(s, c) for s, c in groups.items() if s == SOURCE_DISCORD]
    external = [(s, c) for s, c in groups.items() if s != SOURCE_DISCORD]
    return corpus + external


def _capped(
    groups: list[tuple[str, list[Citation]]], limit: int
) -> list[tuple[str, list[Citation]]]:
    """Trim to `limit` citations by taking a turn from each source in rotation.

    Trimming the flat list instead would let a run with five channel hits and
    one search result publish a reply that cites only the channel while its
    prose rests partly on the web -- which is the exact thing the reader must
    be able to see. A source that contributed to the answer keeps a line.
    """
    kept: dict[str, list[Citation]] = {source: [] for source, _ in groups}
    remaining = limit
    for row in zip_longest(*(group for _, group in groups)):
        for (source, _), citation in zip(groups, row, strict=True):
            if citation is not None and remaining:
                kept[source].append(citation)
                remaining -= 1
    return [(source, kept[source]) for source, _ in groups if kept[source]]


def _render(scoped: ScopedAnswer) -> str:
    answer = scoped.answer
    body = answer.text[:MAX_REPLY_CHARS]
    if not answer.citations:
        return body
    lines = [body]
    number = 1
    for source, group in _capped(_grouped(answer.citations), MAX_CITATIONS):
        lines.extend(["", SOURCE_HEADINGS.get(source, f"**From {source}:**")])
        for citation in group:
            lines.append(_citation_line(number, citation))
            number += 1
    return "\n".join(lines)


class CyberFriendClient(discord.Client):
    def __init__(self, asks: AskService, guild_id: int) -> None:
        intents = discord.Intents.default()
        # Reading what people actually said, and enumerating who can read a
        # channel. The second is what makes audience containment exact rather
        # than an approximation over role sets.
        intents.message_content = True
        intents.members = True
        # Answers quote text written by anyone in the server. Without this,
        # an excerpt containing @everyone pings the whole server using the
        # bot's permissions -- the poster needs no mention permission of
        # their own, only the bot's.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self._asks = asks
        self._guild_id = guild_id
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self._guild_id)
        self.tree.add_command(self._build_ask_command(), guild=guild)
        await self.tree.sync(guild=guild)

    def _build_ask_command(self) -> app_commands.Command[Any, ..., None]:
        @app_commands.command(name="ask", description="Ask about what's been said")
        @app_commands.describe(question="What do you want to know?")
        async def ask(interaction: discord.Interaction, question: str) -> None:
            # A reasoning run can outlast Discord's 3s interaction deadline,
            # so acknowledge immediately and deliver when ready.
            await interaction.response.defer(thinking=True)
            destination = (
                ChannelRef(PLATFORM, interaction.channel_id)
                if interaction.guild_id and interaction.channel_id
                else None
            )
            outcome = await self._asks.ask(
                AskRequest(
                    asker=_person(interaction.user),
                    text=question,
                    destination=destination,
                    location_id=interaction.channel_id or interaction.user.id,
                )
            )
            if outcome.rate_limited:
                await interaction.followup.send(
                    f"You've hit your question limit. Try again in "
                    f"{int(outcome.retry_after_seconds // 60) + 1} minute(s).",
                    ephemeral=True,
                )
                return

            assert outcome.scoped is not None
            await interaction.followup.send(_render(outcome.scoped))
            await self._notify_if_withheld(outcome.scoped, interaction.user, interaction.channel)

        return ask

    async def on_ready(self) -> None:
        log.info("bot.ready", user=str(self.user), guilds=len(self.guilds))

    async def on_message(self, message: discord.Message) -> None:
        # Never answer ourselves, and never ingest our own output as evidence.
        if message.author.bot:
            return

        is_dm = message.guild is None
        if not is_dm and not self.user_mentioned(message):
            return

        text = self._strip_mention(message.content).strip()
        if not text:
            await message.reply(CAPABILITIES, mention_author=False)
            return

        destination = ChannelRef(PLATFORM, message.channel.id) if not is_dm else None
        async with message.channel.typing():
            outcome = await self._asks.ask(
                AskRequest(
                    asker=_person(message.author),
                    text=text,
                    destination=destination,
                    location_id=message.channel.id,
                )
            )

        if outcome.rate_limited:
            await message.reply(
                f"You've hit your question limit. Try again in "
                f"{int(outcome.retry_after_seconds // 60) + 1} minute(s).",
                mention_author=False,
            )
            return

        assert outcome.scoped is not None
        await message.reply(_render(outcome.scoped), mention_author=False)
        await self._notify_if_withheld(outcome.scoped, message.author, message.channel)

    def user_mentioned(self, message: discord.Message) -> bool:
        return self.user is not None and self.user in message.mentions

    def _strip_mention(self, content: str) -> str:
        if self.user is None:
            return content
        return content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "")

    async def _notify_if_withheld(
        self,
        scoped: ScopedAnswer,
        user: discord.User | discord.Member,
        channel: object,
    ) -> None:
        """Tell the asker privately that a fuller answer exists.

        Sent by DM so the channel learns nothing -- "there is more here you
        cannot see" would itself disclose that private content exists.
        """
        if not scoped.should_notify_asker:
            return
        name = getattr(channel, "name", "this channel")
        try:
            await user.send(withheld_notice(f"#{name}"))
        except discord.Forbidden:
            # DMs closed. Staying silent is correct: the alternative is
            # saying it in the channel, which is the disclosure we avoid.
            log.info("notice.dm_blocked", user_id=user.id)
