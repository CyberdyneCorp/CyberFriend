"""The Discord surface.

Identity is free here: Discord authenticates the author of every message, so
there is no client-supplied viewer parameter to forge. Content claiming to
come from someone else is disregarded -- only the authenticated author counts.
"""

from __future__ import annotations

from typing import Any

import discord
import structlog
from discord import app_commands

from chatmemory.app.ask import AskRequest, AskService
from chatmemory.app.disclosure import ScopedAnswer, withheld_notice
from chatmemory.domain.identity import ChannelRef, PersonRef

log = structlog.get_logger()

PLATFORM = "discord"
MAX_REPLY_CHARS = 1900  # Discord's limit is 2000; leave room for citations.

CAPABILITIES = (
    "I answer questions about what's been said in the channels you can read.\n"
    "Try: `what did people ask me today?`, `what happened in #infra this week?`\n"
    "Mention me with a question, use `/ask`, or send me a direct message.\n\n"
    "Answers posted in a channel only use sources everyone here can read. "
    "Ask me in a DM to search everything *you* can read."
)


def _person(user: discord.User | discord.Member) -> PersonRef:
    return PersonRef(PLATFORM, user.id)


def _render(scoped: ScopedAnswer) -> str:
    answer = scoped.answer
    body = answer.text[:MAX_REPLY_CHARS]
    if not answer.citations:
        return body
    lines = [body, ""]
    for n, c in enumerate(answer.citations[:5], start=1):
        # Never an empty label: markdown renders `[]( url )` as a bare URL,
        # and a window spans several people, so naming one author is wrong
        # even when a name is known.
        label = c.author_display.strip() or "jump to message"
        excerpt = " ".join(c.excerpt.split())[:140]
        lines.append(f"{n}. [{label}]({c.url}) — {excerpt}")
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
