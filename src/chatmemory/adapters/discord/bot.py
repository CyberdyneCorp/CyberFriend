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

`/resolve` is here for the same reason the buttons below are. An ask is a
claim the system made about somebody, so the person it named has to be able to
say "done" or "that was never mine" -- and whose word that is has to come from
the interaction, which carries an authenticated account, rather than from
anything they typed. The ask itself is chosen from a list built for that one
account, and a key they supply instead buys them nothing: the store binds both
their readable channels and their own person id as predicates.

The confirmation prompt below is the other thing this file owes a person. It
is shown with buttons rather than by asking them to type a word, and the
reason is the threat model rather than taste: a typed "yes" arrives as message
content, in the same channel the agent indexes, and telling a confirmation
apart from a quotation of one then becomes a parsing problem nobody wins. A
button press arrives as an interaction carrying the account that pressed it,
which is the one thing retrieved text can never forge. It is also always
private -- ephemeral for a slash command, a direct message otherwise --
because a prompt shown in a channel asks everyone present to approve on the
requester's behalf, and the fastest clicker decides.

`/forget` is the person's control over what the assistant remembers of their
conversation. Like `/resolve` it takes whose history from the interaction and
nothing else, and it answers privately: announcing in a channel that somebody
erased what they asked would say something about what they asked.
"""

from __future__ import annotations

import asyncio
from itertools import zip_longest
from typing import Any, Protocol

import discord
import structlog
from discord import app_commands

from chatmemory.app.ask import (
    AskRequest,
    AskService,
    CorrectionRequest,
    ForgetRequest,
    conversation_location,
)
from chatmemory.app.asks.model import (
    AskKind,
    AskStatus,
    CorrectionOutcome,
    CorrectionResolution,
    ReportedAsk,
)
from chatmemory.app.authorization import ConfirmationPrompt
from chatmemory.app.confirmation import ConfirmationReply, Undeliverable
from chatmemory.app.disclosure import ScopedAnswer, withheld_notice
from chatmemory.app.reasoning.evidence import SOURCE_DISCORD, SOURCE_WEB, SourcedCitation
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.ports.answers import Citation

log = structlog.get_logger()

PLATFORM = "discord"
MAX_REPLY_CHARS = 1900  # Discord's limit is 2000; leave room for citations.
MAX_CITATIONS = 5
MAX_EXCERPT_CHARS = 140
MAX_ARGUMENT_CHARS = 1200  # Leaves room for the rest of the prompt.

NOT_YOUR_CONFIRMATION = (
    "That's not yours to approve — only the person who asked can confirm it."
)

APPROVED_NOTE = "Approved. Running it now."
DECLINED_NOTE = "Declined. Nothing was changed."
EXPIRED_NOTE = (
    "This request timed out and nothing was changed. Ask again if you still want it."
)

SOURCE_HEADINGS = {
    SOURCE_DISCORD: "**From this server:**",
    SOURCE_WEB: "**From the web:**",
}

SOURCE_FALLBACK_LABELS = {
    SOURCE_DISCORD: "jump to message",
    SOURCE_WEB: "open result",
}

# Discord's own limits on a slash-command choice. A label past the first is
# rejected outright, which would take the whole autocomplete down with it.
MAX_CHOICE_CHARS = 100
MAX_CORRECTABLE = 25

#: What each answer to "what happened to it" means on the record. Two
#: resolutions, three things a person might say: "not mine" and "that was never
#: a request" are both the system having been wrong about them, and both stop
#: it being their obligation. Keeping the wording separate is for the person
#: reading the menu; the record only needs to know it was dismissed.
CORRECTION_RESOLUTIONS = {
    "done": CorrectionResolution.DONE,
    "not_mine": CorrectionResolution.NOT_APPLICABLE,
    "not_a_request": CorrectionResolution.NOT_APPLICABLE,
}

CORRECTION_APPLIED = {
    "done": "Marked done. It won't come up again.",
    "not_mine": "Noted — not yours. I won't raise it against you again.",
    "not_a_request": "Noted — not a request. I won't raise it again.",
}

# One refusal for both ways this can fail. `UNKNOWN_ASK` and `NOT_ADDRESSEE`
# are different facts, and telling them apart would answer, for any key
# somebody cared to try, whether an ask exists in a channel they cannot read.
CORRECTION_REFUSED = (
    "I can't close that one. Either there's no such ask, or it isn't yours to close."
)

FORGET_HERE = "here"
FORGET_EVERYWHERE = "everywhere"

MEMORY_UNAVAILABLE = "I don't keep conversation history here, so there's nothing to forget."


def _forgotten_note(turns: int, summaries: int, everywhere: bool) -> str:
    where = "everywhere" if everywhere else "in this conversation"
    if not turns and not summaries:
        return f"There was nothing to forget {where}."
    return f"Done. I've forgotten what you asked me {where}."


CAPABILITIES = (
    "I answer questions about what's been said in the channels you can read.\n"
    "Try: `what did people ask me today?`, `what happened in #infra this week?`\n"
    "Mention me with a question, use `/ask`, or send me a direct message.\n"
    "Use `/resolve` to close something I said was asked of you, or to tell me "
    "it was never yours.\n"
    "I remember our conversation so follow-ups make sense; `/forget` erases it.\n\n"
    "Answers posted in a channel only use sources everyone here can read. "
    "Ask me in a DM to search everything *you* can read."
)


def _person(user: discord.User | discord.Member) -> PersonRef:
    return PersonRef(PLATFORM, user.id)


def forget_request(
    requester: PersonRef, *, channel_id: int | None, in_guild: bool, everywhere: bool
) -> ForgetRequest:
    """Name the location exactly as `/ask` and a mention remember under.

    Both key a conversation by the channel the question arrived in, and mark
    it direct when there is no guild. A `/forget` that spelled it any other way
    would report success over a conversation it never touched.
    """
    if everywhere:
        return ForgetRequest(requester, None)
    location_id = channel_id if channel_id is not None else requester.platform_user_id
    # The same test `/ask` applies to decide a reply is not posted to a channel.
    direct = not (in_guild and channel_id is not None)
    return ForgetRequest(
        requester, conversation_location(requester, location_id, direct=direct)
    )


def _source_of(citation: Citation) -> str:
    """Where a citation came from.

    `Citation` is the port and describes a place, not a kind; evidence that
    came from outside the corpus attaches the kind on the way through. A
    plain `Citation` is the corpus, which is what every caller that predates
    egress produces.
    """
    return citation.source_system if isinstance(citation, SourcedCitation) else SOURCE_DISCORD


def _clip(text: str, limit: int) -> str:
    """Cut at a word boundary, and say that it was cut.

    A hard slice ends mid-word -- "is schedu" -- which reads as a rendering
    fault rather than an excerpt, and invites the reader to wonder whether
    the source itself is damaged. The ellipsis is the difference between a
    quotation and a glitch.
    """
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    spaced = cut.rsplit(" ", 1)[0]
    # A single very long token has no boundary to fall back to; clip it
    # rather than return nothing.
    return (spaced if len(spaced) >= limit // 2 else cut).rstrip(" ,.;:") + "\u2026"


def _citation_line(number: int, citation: Citation) -> str:
    source = _source_of(citation)
    # Never an empty label: markdown renders `[]( url )` as a bare URL, and a
    # window spans several people, so naming one author is wrong even when a
    # name is known.
    label = citation.author_display.strip() or SOURCE_FALLBACK_LABELS.get(source, source)
    excerpt = _clip(" ".join(citation.excerpt.split()), MAX_EXCERPT_CHARS)
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


_ASK_PHRASING = {
    AskKind.REQUEST: "asked you to",
    AskKind.QUESTION: "asked you",
    AskKind.COMMITMENT: "you said you would",
}


def _ask_label(item: ReportedAsk) -> str:
    """One line naming an ask well enough to pick it out of a menu.

    The extracted text is model-written and the display name came from the
    platform, so both are flattened to a single line before they go anywhere
    near a choice label: a newline in a label is how a menu entry turns into
    two, and the second one reads as the bot speaking.
    """
    ask = item.ask
    when = ask.asked_at.date().isoformat()
    stale = " (stale)" if ask.status is AskStatus.STALE else ""
    if ask.kind is AskKind.COMMITMENT:
        body = f"{_ASK_PHRASING[ask.kind]} {ask.text}"
    else:
        body = f"{item.requester_display} {_ASK_PHRASING[ask.kind]} {ask.text}"
    return _clip(" ".join(f"{when}: {body}{stale}".split()), MAX_CHOICE_CHARS)


def _correction_note(outcome: CorrectionOutcome, said: str) -> str:
    if outcome is CorrectionOutcome.APPLIED:
        return CORRECTION_APPLIED[said]
    # Every other outcome, deliberately collapsed. See CORRECTION_REFUSED.
    return CORRECTION_REFUSED


def _prompt_text(prompt: ConfirmationPrompt) -> str:
    """What the requester reads before approving a change to another system.

    The tool, the system and the exact arguments, because approving "the
    issue tool" is not approving anything: the argument text is what decides
    which issue is closed and what is written into it.

    Those arguments are model-written and have just shared a context with
    retrieved messages, so they are rendered as fenced data with any literal
    fence in them defanged. A payload that closes the block early would
    otherwise be reading as prose to the person deciding.
    """
    arguments = prompt.arguments_rendered[:MAX_ARGUMENT_CHARS].replace("```", "` ` `")
    return (
        f"**{prompt.qualified_name}** on `{prompt.server}` changes something "
        "outside this server. It would be called with:\n"
        f"```json\n{arguments}\n```\n"
        "Approve only if you asked for this. The approval covers these exact "
        "arguments and this call alone."
    )


class _EditableMessage(Protocol):
    """The one thing this file does with a sent prompt: redraw it.

    A protocol rather than `Message | WebhookMessage`, because a direct
    message and an ephemeral followup have no common supertype and the
    difference does not matter here.
    """

    async def edit(self, *, content: str, view: discord.ui.View) -> object: ...


class _ApprovalView(discord.ui.View):
    """Two buttons, answerable by one account, for a bounded time.

    `requester_id` is taken from the prompt rather than from whoever the
    message was sent to: the prompt says whose decision this is, and that is
    the only account `interaction_check` will accept.
    """

    def __init__(self, requester_id: int, window_seconds: float) -> None:
        super().__init__(timeout=window_seconds)
        self._requester_id = requester_id
        self._window_seconds = window_seconds
        # The wait is on an event of our own rather than on `View.wait()`.
        # discord.py only starts a view's timer once the message reaches its
        # store, so a view whose send failed -- or one under test -- would
        # otherwise wait for a timeout that was never scheduled.
        self._settled = asyncio.Event()
        # The sent message, once there is one, so the prompt can be retired
        # when the run stops waiting. None under test and whenever the send
        # returned nothing; the window is enforced either way.
        self._message: _EditableMessage | None = None
        self._closed = False
        self.reply: ConfirmationReply | None = None

    def sent_as(self, message: _EditableMessage | None) -> None:
        """Remember what the prompt was posted as, so it can be retired."""
        self._message = message

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        """Refuse a click from anyone but the requester.

        Reachable in a way the ephemeral case is not: a direct-message prompt
        lives in a real message, and a shared or forwarded one could in
        principle be pressed by another account. Refusing here means a second
        person's click never becomes a `ConfirmationReply` at all.
        """
        if interaction.user.id == self._requester_id:
            return True
        log.warning(
            "confirmation.button.not_requester",
            requester_id=self._requester_id,
            clicked_by=interaction.user.id,
        )
        try:
            await interaction.response.send_message(NOT_YOUR_CONFIRMATION, ephemeral=True)
        except discord.HTTPException:
            # Saying so is a courtesy; refusing is the requirement.
            log.info("confirmation.button.refusal_not_shown")
        return False

    # Approve is styled as the destructive action because it is the one: the
    # green-button habit is what turns a confirmation into a reflex.
    @discord.ui.button(label="Approve", style=discord.ButtonStyle.danger)
    async def approve(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self._answer(interaction, granted=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def decline(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        await self._answer(interaction, granted=False)

    async def _answer(self, interaction: discord.Interaction, *, granted: bool) -> None:
        """Record the press and take the buttons away.

        Disabling them is not decoration. One approval authorises one call, and
        a live button on a settled decision is an invitation to authorise a
        second one that nobody is watching for.

        A press that arrives after the window is answered honestly instead. It
        authorises nothing -- the run stopped waiting and would refuse it -- so
        telling this person "Approved" would have them believe a change
        happened that cannot now happen.
        """
        self._disable_buttons()
        if self._closed:
            await self._redraw(interaction, EXPIRED_NOTE)
            return
        self.reply = ConfirmationReply(_person(interaction.user), granted)
        await self._redraw(interaction, APPROVED_NOTE if granted else DECLINED_NOTE)
        self._settled.set()
        self.stop()

    def _disable_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _redraw(self, interaction: discord.Interaction, note: str) -> None:
        try:
            await interaction.response.edit_message(content=note, view=self)
        except discord.HTTPException:
            # The answer is already held; failing to redraw must not lose it.
            log.info("confirmation.button.edit_failed", note=note)

    async def settled(self) -> ConfirmationReply | None:
        """The requester's answer, or None if the window closed on silence."""
        try:
            await asyncio.wait_for(self._settled.wait(), timeout=self._window_seconds)
        except TimeoutError:
            log.info("confirmation.button.window_closed", requester_id=self._requester_id)
            self._closed = True
            self.stop()
            await self._expire()
            return None
        return self.reply

    async def _expire(self) -> None:
        """Retire a prompt the run has stopped waiting on.

        A live Approve button on an abandoned decision is a promise the system
        can no longer keep: the press would record nothing and change nothing,
        while reading to the person as consent that was acted on.
        """
        if self._message is None or self.reply is not None:
            return
        self._disable_buttons()
        try:
            await self._message.edit(content=EXPIRED_NOTE, view=self)
        except discord.HTTPException:
            # The window is what refuses the call; this is only the notice.
            log.info("confirmation.button.expiry_not_shown")


class EphemeralConfirmation:
    """The prompt for a slash command: a followup only the requester can see.

    Ephemeral rather than a direct message because the person is already here
    and waiting on this interaction, and because it needs no open DMs to
    arrive. The answer is still delivered publicly; only the question about
    changing something is private.
    """

    def __init__(self, interaction: discord.Interaction) -> None:
        self._interaction = interaction

    async def ask(
        self, prompt: ConfirmationPrompt, window_seconds: float
    ) -> ConfirmationReply | None:
        view = _ApprovalView(prompt.requester.platform_user_id, window_seconds)
        try:
            # `wait=True` is what an interaction followup does anyway; asking
            # for it explicitly is what hands back the message, so an
            # unanswered prompt can be retired rather than left live.
            sent = await self._interaction.followup.send(
                _prompt_text(prompt),
                view=view,
                ephemeral=True,
                wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            raise Undeliverable(f"ephemeral prompt not delivered: {exc}") from exc
        view.sent_as(sent)
        return await view.settled()


class DirectMessageConfirmation:
    """The prompt for a mention or a DM: sent to the person, never to the room.

    A mention is answered in the channel, so this is the case the requirement
    is really about -- posting "shall I close issue 42?" where the question
    was asked hands the decision to whoever is reading. If their DMs are
    closed the prompt is undeliverable and the call simply does not happen;
    the alternative is asking the room, which is the thing being avoided.
    """

    def __init__(self, user: discord.User | discord.Member) -> None:
        self._user = user

    async def ask(
        self, prompt: ConfirmationPrompt, window_seconds: float
    ) -> ConfirmationReply | None:
        view = _ApprovalView(prompt.requester.platform_user_id, window_seconds)
        try:
            sent = await self._user.send(
                _prompt_text(prompt),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden as exc:
            log.info("confirmation.dm_blocked", user_id=self._user.id)
            raise Undeliverable("the requester's direct messages are closed") from exc
        except discord.HTTPException as exc:
            raise Undeliverable(f"direct message not delivered: {exc}") from exc
        view.sent_as(sent)
        return await view.settled()


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
        # Registered unconditionally, next to `/ask`. A list that only ever
        # grows is one people stop reading, so the way out of it is not a
        # thing to make conditional on configuration somebody has to find.
        self.tree.add_command(self._build_resolve_command(), guild=guild)
        # Registered unconditionally for the same reason: memory ships with its
        # off switch, and a way out that depends on configuration is one that
        # is missing on the deployment somebody needs it on.
        self.tree.add_command(self._build_forget_command(), guild=guild)
        await self.tree.sync(guild=guild)

    def _build_forget_command(self) -> app_commands.Command[Any, ..., None]:
        """`/forget`: erase the caller's own conversation, here or everywhere."""

        @app_commands.command(
            name="forget", description="Erase what I remember of our conversation"
        )
        @app_commands.describe(scope="Just this conversation, or everywhere")
        @app_commands.choices(
            scope=[
                app_commands.Choice(name="This conversation", value=FORGET_HERE),
                app_commands.Choice(name="Everywhere", value=FORGET_EVERYWHERE),
            ]
        )
        async def forget(
            interaction: discord.Interaction, scope: app_commands.Choice[str]
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            request = forget_request(
                _person(interaction.user),
                channel_id=interaction.channel_id,
                in_guild=interaction.guild_id is not None,
                everywhere=scope.value == FORGET_EVERYWHERE,
            )
            purge = await self._asks.forget(request)
            note = (
                MEMORY_UNAVAILABLE
                if purge is None
                else _forgotten_note(purge.turns, purge.summaries, request.location is None)
            )
            await interaction.followup.send(note, ephemeral=True)

        return forget

    def _build_resolve_command(self) -> app_commands.Command[Any, ..., None]:
        """`/resolve`: the addressee's own word about their own ask.

        Always ephemeral, in both directions. The menu is a list of things
        somebody was asked to do, which is nobody else's business even when
        the command is run in a busy channel, and a public refusal would
        announce that they tried.
        """

        @app_commands.command(
            name="resolve", description="Close or dismiss something I said was asked of you"
        )
        @app_commands.describe(
            ask="Which one — pick from your own outstanding asks",
            outcome="What actually happened to it",
        )
        @app_commands.choices(
            outcome=[
                app_commands.Choice(name="Done", value="done"),
                app_commands.Choice(name="Not mine", value="not_mine"),
                app_commands.Choice(name="This was never a request", value="not_a_request"),
            ]
        )
        async def resolve(
            interaction: discord.Interaction,
            ask: str,
            outcome: app_commands.Choice[str],
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            said = outcome.value
            result = await self._asks.correct(
                CorrectionRequest(
                    # From the interaction, never from `ask`: the key is a
                    # string this person typed or picked, and a string is
                    # content. Only Discord can say who sent it.
                    actor=_person(interaction.user),
                    ask_key=ask,
                    resolution=CORRECTION_RESOLUTIONS[said],
                )
            )
            await interaction.followup.send(_correction_note(result, said), ephemeral=True)

        @resolve.autocomplete("ask")
        async def which(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            """Offer this account its own outstanding asks and nothing else.

            The filtering is a convenience, not the control: whatever comes
            back here, and whatever somebody types instead of picking from it,
            is checked again against their readable channels and their own
            person id when the correction is written.
            """
            found = await self._asks.correctable(
                _person(interaction.user), limit=MAX_CORRECTABLE
            )
            typed = current.strip().casefold()
            return [
                app_commands.Choice(name=_ask_label(item), value=item.ask.key)
                for item in found
                # A key Discord would reject takes the whole menu down with
                # it, so it is dropped rather than sent and failed on.
                if len(item.ask.key) <= MAX_CHOICE_CHARS
                and (not typed or typed in _ask_label(item).casefold())
            ]

        return resolve

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
                ),
                # Where a mutating tool would ask this person for permission.
                # Passed for every question, not only ones that look like
                # they might act: which tools a run reaches is decided inside
                # the run, and a surface that is only supplied when the
                # adapter guesses right is a surface that is absent when it
                # guesses wrong.
                EphemeralConfirmation(interaction),
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
                ),
                # A DM even when the question was asked in a channel: the
                # answer's destination is not the prompt's, because a prompt
                # in the channel would ask the room to decide for the asker.
                DirectMessageConfirmation(message.author),
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
