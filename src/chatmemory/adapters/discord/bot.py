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
from collections.abc import Awaitable, Callable, Sequence
from itertools import zip_longest
from typing import Any, Protocol

import discord
import structlog
from discord import app_commands

from chatmemory.adapters.discord.formatting import (
    escape_markdown,
    masked_link,
    sanitize_answer,
    split_message,
)
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
from chatmemory.app.asks.obligations import MessageUrl
from chatmemory.app.authorization import ConfirmationPrompt
from chatmemory.app.channel_listing import ChannelListing, ChannelListingService
from chatmemory.app.confirmation import ConfirmationReply, Undeliverable
from chatmemory.app.disclosure import ScopedAnswer, withheld_notice
from chatmemory.app.indexing import (
    ChannelAccess,
    IndexAction,
    IndexingService,
    IndexRequest,
)
from chatmemory.app.notifications import NotificationPreferences
from chatmemory.app.reasoning.evidence import SOURCE_DISCORD, SOURCE_WEB, SourcedCitation
from chatmemory.app.schedules import CreateRefusal, CreateResult, ScheduleService
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.ports.answers import Citation
from chatmemory.ports.notifications import (
    DeliveryResult,
    NotificationDraft,
    PendingNotification,
)
from chatmemory.ports.schedules import (
    MAX_INTERVAL_HOURS,
    MIN_INTERVAL_HOURS,
    ScheduledTask,
    TaskOutcome,
)

UNPLACEABLE_INTERACTION = (
    "I couldn't tell which channel this was asked in, so I didn't answer. "
    "Try again from the channel, or send me a direct message."
)
log = structlog.get_logger()

PLATFORM = "discord"
# An answer is split across messages rather than cut, but not without bound:
# a runaway answer should not become a wall of messages in a shared channel.
MAX_ANSWER_CHARS = 6000
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

# Sources are subtext (`-# `): present on every answer, and quieter than it.
SOURCE_HEADINGS = {
    SOURCE_DISCORD: "-# **From this server:**",
    SOURCE_WEB: "-# **From the web:**",
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

SCHEDULE_DELETED = "Stopped. I won't ask that again."

SCHEDULE_NOT_YOURS = (
    "I don't have a scheduled task with that number for you. "
    "`/schedule list` shows yours."
)
"""Said both when the task is somebody else's and when it does not exist.

Two sentences would make this command a way to learn which task numbers are
real, which is the same disclosure `/channels` refuses to make about
channels."""


def _created_message(result: CreateResult) -> str:
    if result.task is not None:
        task = result.task
        return (
            f"Done. I'll ask that every **{task.interval_hours}h**, "
            f"starting {discord.utils.format_dt(task.next_run_at, 'R')}.\n"
            "I'll only message you when I find something, so silence means "
            "nothing new. `/schedule list` shows when each one last ran."
        )
    if result.refusal is CreateRefusal.INTERVAL_OUT_OF_RANGE:
        return (
            f"I can ask between every **{MIN_INTERVAL_HOURS}h** and every "
            f"**{MAX_INTERVAL_HOURS}h**."
        )
    if result.refusal is CreateRefusal.EMPTY_QUESTION:
        return "Tell me what to ask."
    return (
        "You've reached the number of scheduled questions I can keep for one "
        "person. Delete one with `/schedule delete` first."
    )


def _schedule_listing(tasks: Sequence[ScheduledTask]) -> str:
    if not tasks:
        return (
            "You have no scheduled questions. `/schedule create` sets one up.\n"
            "I'll only message you when there's something to say."
        )
    lines = [f"**Your scheduled questions ({len(tasks)}):**"]
    for task in tasks:
        lines.append(f"**{task.id}** - every {task.interval_hours}h - {task.question}")
        lines.append(f"  {_task_state(task)}")
    return "\n".join(lines)


def _task_state(task: ScheduledTask) -> str:
    """When it last ran and what happened, so silence is legible.

    The whole reason this exists: a task that has run eleven times and found
    nothing is working, and without this it is indistinguishable from one that
    stopped running in March.
    """
    if not task.active:
        return f"stopped - {task.disabled_reason or 'disabled'}"
    if task.last_run_at is None:
        return f"not run yet - first {discord.utils.format_dt(task.next_run_at, 'R')}"
    when = discord.utils.format_dt(task.last_run_at, "R")
    outcome = {
        TaskOutcome.REPORTED: "found something and messaged you",
        TaskOutcome.NOTHING: "found nothing",
        TaskOutcome.FAILED: "failed",
        TaskOutcome.CLOSED: "couldn't reach your direct messages",
    }.get(task.last_outcome or TaskOutcome.NOTHING, "ran")
    return f"last ran {when}, {outcome} - next {discord.utils.format_dt(task.next_run_at, 'R')}"


SCHEDULED_PREFIX = "⏰ **Scheduled**"
"""Marks an answer nobody just asked for.

Without it a direct message arriving at 3am reads as the assistant volunteering
something, which is the one thing this project is careful never to do
unannounced."""

SCHEDULES_UNAVAILABLE = (
    "I can't manage scheduled tasks here - the feature isn't switched on for "
    "this deployment."
)

CHANNELS_UNAVAILABLE = (
    "I can't list archived channels here - indexing isn't wired up on this "
    "deployment."
)

NO_CHANNELS_FOR_YOU = (
    "There are no archived channels you can read.\n"
    "Someone with Manage Channels on a channel can archive it with `/index`."
)
"""Said whether the server archives nothing or archives only channels this
person cannot read. The two must be indistinguishable: a different wording for
each would make the reply a test for whether private archives exist."""


def _channels_message(listing: ChannelListing) -> str:
    if listing.empty:
        return NO_CHANNELS_FOR_YOU
    lines = [
        f"**Archived channels you can read ({len(listing.channels)}):**",
        *(f"- <#{c.platform_channel_id}>" for c in listing.channels),
        "",
        "Everything said in these is stored so people who can read them can "
        "search it. `/unindex` stops one and deletes what was archived.",
    ]
    return "\n".join(lines)


INDEXING_UNAVAILABLE = (
    "Indexing can't be changed from Discord on this deployment. "
    "Ask an operator to use the admin console."
)

INDEXING_DESCRIPTIONS = {
    IndexAction.INDEX: "Archive a channel so people who can read it can search it",
    IndexAction.UNINDEX: "Stop archiving a channel and delete what was archived",
}


# --- notifications ------------------------------------------------------
#
# The only place in this file that produces a message nobody asked for, so
# the wording carries more weight than usual. A person receiving one has not
# opted into anything: they were named in somebody else's request, and the
# assistant decided that was worth interrupting them for. So the message says
# who asked, where, and links to the thing itself, and quotes only enough to
# recognise it -- the archive is not re-published into a direct message.

#: How the command to stop is spelled. One string, used in the command
#: definition and in the sentence that tells people about it, because a way
#: out that is described differently from how it is invoked is not a way out.
NOTIFICATIONS_COMMAND = "/notifications"

NOTIFICATIONS_UNAVAILABLE = (
    "Notifications aren't configured on this deployment, so there's nothing "
    "to turn on or off."
)

NOTIFICATIONS_TURNED_OFF = (
    "Done — I won't message you about things people ask of you. "
    f"`{NOTIFICATIONS_COMMAND} on` brings them back."
)

NOTIFICATIONS_TURNED_ON = (
    "Done — I'll message you when someone asks you for something. "
    f"`{NOTIFICATIONS_COMMAND} off` stops it again."
)

#: Said in the first message somebody ever receives, and only there. Every
#: message after it would be noise; the first one has to carry the exit,
#: because a person who cannot find the off switch will block the bot instead.
HOW_TO_STOP = (
    "-# You're getting this because someone addressed a request to you in a "
    f"channel you can read. `{NOTIFICATIONS_COMMAND} off` stops these."
)

NOTIFICATION_OPENING = {
    1: "Someone asked you for something:",
}
NOTIFICATION_OPENING_MANY = "{count} things were asked of you:"

#: How each kind of obligation is phrased. Commitments never appear here: an
#: ask is only queued when the addressee is somebody other than the person who
#: spoke, and a commitment is addressed to its own speaker.
NOTIFICATION_PHRASING = {
    AskKind.REQUEST: "asked you to",
    AskKind.QUESTION: "asked you",
    AskKind.COMMITMENT: "noted that you would",
}

#: The excerpt in a notification is shorter than the one in an answer. A
#: notification is a pointer to a message, not a delivery of it: the link is
#: how somebody reads the thing, and a long quotation here would republish
#: channel content into a direct message nobody asked for.
NOTIFICATION_EXCERPT_CHARS = 120

NOTIFICATION_LINK_LABEL = "open the message"


def _forgotten_note(turns: int, summaries: int, everywhere: bool) -> str:
    if everywhere:
        # Said whatever the counts: forgetting everywhere also deletes the
        # person's facts, which the purge does not count, so "nothing to
        # forget" could be false.
        return (
            "Done. I've forgotten what you asked me everywhere, and anything you "
            "asked me to remember about you."
        )
    if not turns and not summaries:
        return "There was nothing to forget in this conversation."
    return "Done. I've forgotten what you asked me in this conversation."


CAPABILITIES = (
    "I answer questions about what's been said in the channels you can read.\n"
    "Try: `what did people ask me today?`, `what happened in #infra this week?`\n"
    "Mention me with a question, use `/ask`, or send me a direct message.\n"
    "Use `/resolve` to close something I said was asked of you, or to tell me "
    "it was never yours.\n"
    "I remember our conversation so follow-ups make sense; `/forget` erases it.\n"
    "If someone asks you for something in a channel you can read, I'll send "
    "you one direct message about it; `/notifications off` stops that.\n"
    "Tell me `call me Leo`, your email, or `reply to me in Portuguese` and I'll "
    "remember it; your email is only ever shown to you, in a DM.\n\n"
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
    # Clipped before escaping, so the cut never lands between a backslash and
    # the character it escapes. The excerpt is quoted content: escaped so it
    # cannot add a heading, a spoiler, a code fence or a masked link.
    excerpt = escape_markdown(_clip(" ".join(citation.excerpt.split()), MAX_EXCERPT_CHARS))
    # The heading above already says where this came from; the inline tag
    # repeats it for every non-corpus line, because a line quoted, screenshot
    # or read on its own loses the heading and keeps the claim.
    tag = "" if source == SOURCE_DISCORD else f"({source}) "
    target = _link_target(citation.url)
    if not target:
        # `[label]()` renders as literal brackets, which reads as a broken
        # link rather than as a source that has none -- a remote MCP server's
        # result, typically. Say what it is instead.
        return f"-# {number}. {tag}{escape_markdown(label)} — {excerpt}"
    # The one place a reply carries a masked link: built here from the
    # citation's own URL, never taken from answer prose.
    return f"-# {number}. {tag}{masked_link(label, target)} — {excerpt}"


def _link_target(url: str) -> str:
    """A URL that survives being the target of a markdown link.

    Discord ends a link target at the first `)`, so a Wikipedia article like
    `Dune_(novel)` opened `Dune_(novel` -- a working-looking link to the wrong
    page. Parentheses, spaces and angle brackets are percent-encoded, which
    every server decodes back to the same path.
    """
    stripped = url.strip()
    if not stripped:
        return ""
    return (
        stripped.replace(" ", "%20")
        .replace("(", "%28")
        .replace(")", "%29")
        .replace("<", "%3C")
        .replace(">", "%3E")
    )


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
    """The whole reply as markdown, before it is split into messages.

    The answer text is model output written after reading channel messages
    and web pages, so its links are unmasked; only the citation lines built
    below may carry a masked link.
    """
    answer = scoped.answer
    body = sanitize_answer(answer.text[:MAX_ANSWER_CHARS])
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


def _messages(scoped: ScopedAnswer) -> list[str]:
    """The reply as Discord messages, split outside code blocks."""
    return split_message(_render(scoped))


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


class _GuildPermissions(Protocol):
    manage_channels: bool
    view_channel: bool
    read_message_history: bool
    send_messages: bool


class _GuildChannel(Protocol):
    id: int
    name: str

    def permissions_for(self, obj: Any, /) -> _GuildPermissions: ...


class _IndexingGuild(Protocol):
    id: int

    @property
    def me(self) -> Any: ...

    @property
    def text_channels(self) -> Sequence[Any]: ...

    def get_member(self, user_id: int, /) -> Any: ...


class DiscordChannelAccess:
    """Permissions for `/index`, resolved from the guild as it is right now.

    The inputs are an authenticated account and a channel id; the member, the
    channel and both permission sets are looked up in live guild state. A
    channel outside this guild's text channels, or a requester who is not a
    member, resolves to nothing the requester can use -- fail closed.
    """

    def __init__(self, guild: Callable[[], _IndexingGuild | None]) -> None:
        self._guild = guild

    async def resolve(self, requester: PersonRef, channel_id: int) -> ChannelAccess | None:
        guild = self._guild()
        if guild is None or requester.platform != PLATFORM:
            return None
        channel: _GuildChannel | None = next(
            (c for c in guild.text_channels if c.id == channel_id), None
        )
        if channel is None:
            return None
        member = guild.get_member(requester.platform_user_id)
        me = guild.me
        requester_perms = channel.permissions_for(member) if member is not None else None
        assistant = channel.permissions_for(me) if me is not None else None
        return ChannelAccess(
            channel=ChannelRef(PLATFORM, channel.id),
            name=channel.name,
            requester_can_manage=bool(requester_perms and requester_perms.manage_channels),
            assistant_can_view=bool(assistant and assistant.view_channel),
            assistant_can_read_history=bool(assistant and assistant.read_message_history),
            assistant_can_send=bool(assistant and assistant.send_messages),
        )


class DiscordIndexNotifier:
    """Posts the archive notice into the channel itself, visible to every member."""

    def __init__(self, channels: Callable[[int], Any]) -> None:
        self._channels = channels

    async def announce(self, channel: ChannelRef, text: str) -> bool:
        target = self._channels(channel.platform_channel_id)
        if target is None or not hasattr(target, "send"):
            log.warning("indexing.notice_no_channel", channel=str(channel))
            return False
        try:
            await target.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("indexing.notice_not_posted", channel=str(channel))
            return False
        return True


def _notification_line(item: PendingNotification, url: MessageUrl) -> str:
    """One obligation, as a person needs to see it.

    Channel, who asked, what they asked for, a link, and a short quotation --
    in that order, because the first three are what decides whether the reader
    cares and the quotation is only there to make it recognisable.

    Everything drawn from the corpus is escaped. The channel name, the asker's
    display name, the extracted text and the excerpt are all strings somebody
    else chose, and a display name spelled `x](https://evil.example) [y` would
    otherwise rewrite the link sitting next to it.
    """
    try:
        phrasing = NOTIFICATION_PHRASING[AskKind(item.kind)]
    except ValueError:  # pragma: no cover - the store only writes known kinds
        phrasing = "asked you"
    link = masked_link(
        NOTIFICATION_LINK_LABEL, url(item.channel, item.source_message_id)
    )
    line = (
        f"- **#{escape_markdown(item.channel_name)}** — "
        f"{escape_markdown(item.requester_display)} {phrasing}: "
        f"{escape_markdown(item.text)} ({link})"
    )
    excerpt = _clip(item.excerpt, NOTIFICATION_EXCERPT_CHARS)
    if excerpt:
        line += f"\n  -# “{escape_markdown(excerpt)}”"
    return line


def render_notification(draft: NotificationDraft, url: MessageUrl) -> str:
    """The whole batched message.

    A module-level function rather than a method so the wording can be read
    and tested without a gateway connection -- this is the one piece of text
    in the system that arrives uninvited.
    """
    count = len(draft.items) + draft.omitted
    opening = NOTIFICATION_OPENING.get(
        count, NOTIFICATION_OPENING_MANY.format(count=count)
    )
    lines = [opening, *(_notification_line(item, url) for item in draft.items)]
    if draft.omitted:
        # Counted rather than listed: the rest are still queued and are not
        # lost, and a direct message should not become the wall of text this
        # feature exists to save people from.
        lines.append(f"-# …and {draft.omitted} more.")
    if draft.say_how_to_stop:
        lines.append(HOW_TO_STOP)
    return "\n".join(lines)


class DiscordNotificationSender:
    """Delivers one batched message as a direct message.

    Distinguishes "they do not accept direct messages from us" from every
    other failure, because only the first is permanent: Discord answers a
    closed DM with 403, and treating that as transient means retrying a person
    who has already refused, on every pass, for ever.

    The user is looked up through a late-bound callable for the same reason
    every other resolver in this file is: the client's cache does not exist
    until the gateway connects, and a cold cache must read as "not reachable
    yet" rather than as "no such person".
    """

    def __init__(
        self,
        user: Callable[[int], Awaitable[Any]],
        message_url: MessageUrl,
    ) -> None:
        self._user = user
        self._url = message_url

    async def send(self, draft: NotificationDraft) -> DeliveryResult:
        try:
            recipient = await self._user(draft.person.platform_user_id)
        except discord.NotFound:
            # The account no longer exists. Permanent, and shaped exactly like
            # a closed DM from here: stop trying.
            log.info("notifications.user_not_found", person=str(draft.person))
            return DeliveryResult.CLOSED
        except discord.HTTPException:
            log.warning("notifications.user_lookup_failed", person=str(draft.person))
            return DeliveryResult.FAILED
        if recipient is None:
            return DeliveryResult.FAILED

        body = render_notification(draft, self._url)
        try:
            for piece in split_message(body):
                await recipient.send(
                    piece, allowed_mentions=discord.AllowedMentions.none()
                )
        except discord.Forbidden:
            # Their direct messages are closed, or they have blocked the bot.
            # Either way this is them having said no.
            return DeliveryResult.CLOSED
        except discord.HTTPException:
            log.warning("notifications.not_delivered", person=str(draft.person))
            return DeliveryResult.FAILED
        return DeliveryResult.SENT


class DiscordTaskMessenger:
    """Delivers a scheduled task's answer as a direct message.

    Separate from `DiscordNotificationSender` although both send a DM: that one
    renders a notification draft and reports a three-way `DeliveryResult`, and
    this one sends an answer somebody asked for and needs only "did it arrive".
    Sharing them would mean one class serving two vocabularies.

    The 403 handling is the same because the fact is the same: Discord answers
    a closed DM with 403, and treating that as transient means knocking on a
    door that has been shut.
    """

    def __init__(
        self, user: Callable[[int], Awaitable[Any]], prefix: str = SCHEDULED_PREFIX
    ) -> None:
        self._user = user
        # Empty for position alerts, whose text carries its own heading in the
        # language the alert was made in.
        self._prefix = prefix

    async def deliver(self, person: PersonRef, task_id: int, text: str) -> bool:
        try:
            recipient = await self._user(person.platform_user_id)
        except discord.NotFound:
            # The account is gone. Permanent, and shaped like a closed DM.
            log.info("schedules.user_not_found", person=str(person))
            return False
        except discord.HTTPException:
            log.warning("schedules.user_lookup_failed", person=str(person))
            # Transient: reported as undelivered, but the caller only stops a
            # task on a refusal, so this simply means nothing was sent.
            return False
        if recipient is None:
            return False
        try:
            body = f"{self._prefix}\n{text}" if self._prefix else text
            for piece in split_message(body):
                await recipient.send(piece, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            return False
        except discord.HTTPException:
            log.warning("schedules.not_delivered", person=str(person), task_id=task_id)
            return False
        return True


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
        self._indexing: IndexingService | None = None
        self._notifications: NotificationPreferences | None = None
        self._channels: ChannelListingService | None = None
        self._schedules: ScheduleService | None = None

    def attach_indexing(self, indexing: IndexingService) -> None:
        """Give `/index` and `/unindex` somewhere to act.

        Attached after construction because the service's permission resolver
        and notifier read this client's guild cache. Without it both commands
        are still registered and say indexing is unavailable here.
        """
        self._indexing = indexing

    def attach_channel_listing(self, channels: ChannelListingService) -> None:
        """Give `/channels` somewhere to read from.

        Attached after construction like the others, because the resolver it
        reads permissions through is built over this client's guild cache.
        """
        self._channels = channels

    def attach_schedules(self, schedules: ScheduleService) -> None:
        """Give `/schedule` somewhere to read and write.

        Attached after construction like the rest. Without it the group is
        still registered and says the feature is unavailable -- the safe half,
        and the one a deployment that wires nothing should get, because the
        feature sends messages nobody asked for in the moment.
        """
        self._schedules = schedules

    def attach_notifications(self, notifications: NotificationPreferences) -> None:
        """Give `/notifications` somewhere to write.

        Attached after construction, like indexing, because the preference
        store is built over the answer stack's engine rather than from guild
        state. Without it the command is still registered and says
        notifications are unavailable here -- which is the safe half: a
        deployment that wires nothing sends nothing, so there is nothing to
        turn off.
        """
        self._notifications = notifications

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self._guild_id)
        # Commands about the person themselves are global and allowed in a DM
        # with the bot. They were registered on the guild only, and Discord
        # never lists guild commands in a DM -- so "/forget" was missing from
        # the menu exactly where it matters, and typed as text it cannot run.
        # Every one of them is keyed on the interaction's user and already
        # handled a DM; none acts on a channel.
        for command in (
            self._build_ask_command(),
            # Registered unconditionally, next to `/ask`. A list that only ever
            # grows is one people stop reading, so the way out of it is not a
            # thing to make conditional on configuration somebody has to find.
            self._build_resolve_command(),
            # Unconditional for the same reason: memory ships with its off
            # switch, and a way out that depends on configuration is one that
            # is missing on the deployment somebody needs it on.
            self._build_forget_command(),
            # And again, for the strongest version of the same reason: this is
            # the only feature that messages people who did not ask, so the
            # command that stops it must exist on every deployment.
            self._build_notifications_command(),
            # A channel being archived is something everyone in it is owed
            # disclosure about, and disclosure nobody can check is not
            # disclosure.
            self._build_channels_command(),
            # A group rather than three flat commands: `create`, `list` and
            # `delete` are one concept, and Discord shows them together.
            self._build_schedule_group(),
        ):
            self.tree.add_command(_in_guild_and_dm(command))
        # Guild only: these act on a channel. No `default_permissions`: Manage
        # Channels granted by a channel overwrite, and not guild-wide, must
        # still see the command. The permission is checked when it runs.
        for action in IndexAction:
            self.tree.add_command(self._build_indexing_command(action), guild=guild)
        await self.tree.sync()
        # Syncing the guild set also removes the guild copies of the commands
        # that are now global, which would otherwise show twice in the server.
        await self.tree.sync(guild=guild)

    def _build_schedule_group(self) -> app_commands.Group:
        """`/schedule create|list|delete`: a person's own scheduled questions.

        Whose they are comes from the interaction, which carries an
        authenticated account, exactly as `/forget` and `/notifications` do.
        There is no option naming a person, so managing somebody else's tasks
        is not expressible.

        Every reply is private. A scheduled question is a standing statement
        about what somebody is watching, and announcing it in a channel says
        something about them they did not choose to say.
        """
        group = app_commands.Group(
            name="schedule", description="Questions I ask for you on a schedule"
        )

        @group.command(name="create", description="Ask me something on a schedule")
        @app_commands.describe(
            question="What I should ask, in your own words",
            every_hours=f"How often, in hours ({MIN_INTERVAL_HOURS}-{MAX_INTERVAL_HOURS})",
        )
        async def create(
            interaction: discord.Interaction, question: str, every_hours: int
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self._schedules is None:
                await interaction.followup.send(SCHEDULES_UNAVAILABLE, ephemeral=True)
                return
            result = await self._schedules.create(
                _person(interaction.user), question, every_hours
            )
            await interaction.followup.send(
                _created_message(result),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @group.command(name="list", description="Show the questions I ask for you")
        async def listing(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self._schedules is None:
                await interaction.followup.send(SCHEDULES_UNAVAILABLE, ephemeral=True)
                return
            tasks = await self._schedules.list_for(_person(interaction.user))
            await interaction.followup.send(
                _schedule_listing(tasks),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @group.command(name="delete", description="Stop one of your scheduled questions")
        @app_commands.describe(task="The number shown by `/schedule list`")
        async def delete(interaction: discord.Interaction, task: int) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self._schedules is None:
                await interaction.followup.send(SCHEDULES_UNAVAILABLE, ephemeral=True)
                return
            deleted = await self._schedules.delete(_person(interaction.user), task)
            # The same sentence either way. "That is not yours" and "there is
            # no such task" would let somebody learn which numbers exist.
            await interaction.followup.send(
                SCHEDULE_DELETED if deleted else SCHEDULE_NOT_YOURS, ephemeral=True
            )

        return group

    def _build_channels_command(self) -> app_commands.Command[Any, ..., None]:
        """`/channels`: the archived channels this person can read.

        Always private, and never a directory. What is listed is scope
        intersected with the asker's own readable channels; nothing is said
        about what that intersection removed, because "and 4 you cannot see"
        discloses that four private archived channels exist.
        """

        @app_commands.command(
            name="channels", description="List the channels I archive that you can read"
        )
        async def channels(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self._channels is None:
                await interaction.followup.send(CHANNELS_UNAVAILABLE, ephemeral=True)
                return
            listing = await self._channels.for_person(_person(interaction.user))
            await interaction.followup.send(
                _channels_message(listing),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        return channels

    def _build_indexing_command(
        self, action: IndexAction
    ) -> app_commands.Command[Any, ..., None]:
        """`/index` or `/unindex` a channel. Always answered privately.

        The channel is an option Discord resolves; only its id is used, and
        the permissions that decide the request are looked up again from live
        guild state rather than trusted from the interaction payload.
        """

        @app_commands.command(name=action.value, description=INDEXING_DESCRIPTIONS[action])
        @app_commands.describe(channel="The channel to change")
        @app_commands.guild_only()
        async def command(
            interaction: discord.Interaction, channel: discord.TextChannel
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self._indexing is None:
                await interaction.followup.send(INDEXING_UNAVAILABLE, ephemeral=True)
                return
            result = await self._indexing.handle(
                IndexRequest(_person(interaction.user), channel.id, action)
            )
            await interaction.followup.send(result.message, ephemeral=True)

        return command

    def _build_notifications_command(self) -> app_commands.Command[Any, ..., None]:
        """`/notifications on|off`: the person's own switch, and nobody else's.

        Whose preference it is comes from the interaction, which carries an
        authenticated account, and never from anything typed -- exactly as
        `/resolve` and `/forget` do. There is no option for a target person,
        so turning somebody else's notifications off is not expressible.

        Answered privately. Announcing in a channel that somebody switched off
        the assistant's reminders says something about them that they did not
        choose to say.
        """

        @app_commands.command(
            name="notifications",
            description="Turn direct messages about things asked of you on or off",
        )
        @app_commands.describe(setting="Whether I may message you about them")
        @app_commands.choices(
            setting=[
                app_commands.Choice(name="off", value="off"),
                app_commands.Choice(name="on", value="on"),
            ]
        )
        async def notifications(
            interaction: discord.Interaction, setting: app_commands.Choice[str]
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self._notifications is None:
                await interaction.followup.send(
                    NOTIFICATIONS_UNAVAILABLE, ephemeral=True
                )
                return
            person = _person(interaction.user)
            if setting.value == "on":
                await self._notifications.turn_on(person)
                await interaction.followup.send(NOTIFICATIONS_TURNED_ON, ephemeral=True)
                return
            await self._notifications.turn_off(person)
            await interaction.followup.send(NOTIFICATIONS_TURNED_OFF, ephemeral=True)

        return notifications

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
            # A guild interaction without a channel cannot be placed, and must
            # not fall back to "direct message": a DM is the one destination
            # where a person's email may be shown, so guessing DM for a guild
            # question would widen what the answer may contain.
            if interaction.guild_id is not None and interaction.channel_id is None:
                await interaction.followup.send(
                    UNPLACEABLE_INTERACTION,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            destination = (
                ChannelRef(PLATFORM, interaction.channel_id)
                if interaction.guild_id is not None and interaction.channel_id is not None
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
            for part in _messages(outcome.scoped):
                await interaction.followup.send(
                    part, allowed_mentions=discord.AllowedMentions.none()
                )
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
        for index, part in enumerate(_messages(outcome.scoped)):
            if index == 0:
                await message.reply(
                    part, mention_author=False, allowed_mentions=discord.AllowedMentions.none()
                )
            else:
                # A plain send: replying again would stack a reply preview on
                # every part of one answer.
                await message.channel.send(part, allowed_mentions=discord.AllowedMentions.none())
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


_AnyCommand = app_commands.Command[Any, ..., None] | app_commands.Group


def _in_guild_and_dm(command: _AnyCommand) -> _AnyCommand:
    """Usable in the server and in a DM with the bot; installed with the bot only.

    Not user-installable: the bot answers from one server's archive, and a
    person who has not joined it has nothing to ask about.
    """
    command.allowed_contexts = app_commands.AppCommandContext(
        guild=True, dm_channel=True, private_channel=False
    )
    command.allowed_installs = app_commands.AppInstallationType(guild=True, user=False)
    return command
