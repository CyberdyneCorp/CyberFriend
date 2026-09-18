"""The one message in this system that arrives uninvited.

Everything else the assistant says is an answer to a question somebody asked,
and a person reading it has already chosen to be in the conversation. This
message has not been asked for, so what it contains is the whole of the
feature's manners: who asked, where, a link, and a short quotation -- never a
republication of the channel into somebody's direct messages.

It is also the one message whose text is assembled from four strings other
people wrote: a channel name, a display name, the extracted obligation and an
excerpt. All four are data.
"""

from __future__ import annotations

from datetime import UTC, datetime

import discord
import pytest

from chatmemory.adapters.discord.bot import (
    HOW_TO_STOP,
    NOTIFICATION_EXCERPT_CHARS,
    DiscordNotificationSender,
    render_notification,
)
from chatmemory.app.asks.obligations import discord_message_url
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.ports.notifications import (
    DeliveryResult,
    NotificationDraft,
    PendingNotification,
)

GUILD = 55
CHANNEL = ChannelRef("discord", 100)
BOB = PersonRef("discord", 2)
URL = discord_message_url(GUILD)
NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


def an_item(
    key: str = "k1",
    requester: str = "alice",
    channel_name: str = "general",
    text: str = "review the migration",
    excerpt: str = "can you review the migration before friday?",
) -> PendingNotification:
    return PendingNotification(
        ask_key=key,
        channel=CHANNEL,
        channel_name=channel_name,
        source_message_id=7,
        kind="request",
        requester_display=requester,
        text=text,
        excerpt=excerpt,
        asked_at=NOW,
    )


def a_draft(*items: PendingNotification, **kwargs: object) -> NotificationDraft:
    return NotificationDraft(person=BOB, items=items or (an_item(),), **kwargs)  # type: ignore[arg-type]


# --- what a notification says -------------------------------------------


def test_it_names_the_channel_the_asker_and_links_to_the_message() -> None:
    body = render_notification(a_draft(), URL)

    assert "#general" in body
    assert "alice" in body
    assert f"https://discord.com/channels/{GUILD}/100/7" in body


def test_it_quotes_only_a_short_excerpt() -> None:
    """A notification points at a message; it does not deliver one."""
    body = render_notification(a_draft(an_item(excerpt="x" * 1000)), URL)

    assert "x" * 1000 not in body
    assert body.count("x") <= NOTIFICATION_EXCERPT_CHARS


def test_several_obligations_are_one_message_that_says_how_many() -> None:
    body = render_notification(a_draft(an_item("k1"), an_item("k2")), URL)

    assert body.startswith("2 things were asked of you:")
    assert body.count("open the message") == 2


def test_what_was_left_out_is_counted_rather_than_listed() -> None:
    body = render_notification(a_draft(an_item(), omitted=3), URL)

    assert "and 3 more" in body


# --- how to stop --------------------------------------------------------


def test_the_first_message_says_how_to_stop() -> None:
    body = render_notification(a_draft(say_how_to_stop=True), URL)

    assert HOW_TO_STOP in body
    assert "/notifications off" in body


def test_later_messages_do_not_repeat_it() -> None:
    assert HOW_TO_STOP not in render_notification(a_draft(say_how_to_stop=False), URL)


# --- the four strings somebody else wrote -------------------------------


def test_a_hostile_display_name_cannot_rewrite_the_link_beside_it() -> None:
    body = render_notification(
        a_draft(an_item(requester="x](https://evil.example) [y")), URL
    )

    # The address survives as text -- a reader can see what was written -- but
    # every character that would make it a link is escaped, so it renders as
    # the display name it is rather than as somewhere to click.
    assert "](https://evil.example)" not in body
    assert "\\]\\(https://evil.example\\)" in body
    assert f"https://discord.com/channels/{GUILD}/100/7" in body


def test_an_excerpt_cannot_ping_the_server() -> None:
    """The excerpt is quoted with the bot's permissions, not the author's."""
    body = render_notification(a_draft(an_item(excerpt="@everyone standup now")), URL)

    assert "@everyone" not in body


def test_markdown_in_quoted_text_is_neutralised() -> None:
    body = render_notification(
        a_draft(an_item(channel_name="**ops**", text="# ship it")), URL
    )

    assert "**ops**" not in body
    assert "\\#" in body


# --- delivery -----------------------------------------------------------


class _User:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[str] = []

    async def send(self, content: str, **_: object) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(content)


def _response(status: int) -> object:
    class Response:
        def __init__(self) -> None:
            self.status = status
            self.reason = "no"

    return Response()


def a_sender(user: _User | None, error: Exception | None = None) -> DiscordNotificationSender:
    async def fetch(user_id: int) -> object:
        if error is not None:
            raise error
        return user

    return DiscordNotificationSender(fetch, URL)


@pytest.mark.asyncio
async def test_a_batch_is_delivered_as_a_direct_message() -> None:
    user = _User()

    result = await a_sender(user).send(a_draft())

    assert result is DeliveryResult.SENT
    assert "#general" in "".join(user.sent)


@pytest.mark.asyncio
async def test_closed_direct_messages_are_permanent_and_say_so() -> None:
    """403 is the person having said no; retrying it forever is how a bot
    earns a rate limit and a block."""
    user = _User(error=discord.Forbidden(_response(403), "cannot send"))  # type: ignore[arg-type]

    assert await a_sender(user).send(a_draft()) is DeliveryResult.CLOSED


@pytest.mark.asyncio
async def test_a_transient_platform_error_is_not_permanent() -> None:
    user = _User(error=discord.HTTPException(_response(500), "later"))  # type: ignore[arg-type]

    assert await a_sender(user).send(a_draft()) is DeliveryResult.FAILED


@pytest.mark.asyncio
async def test_an_account_that_no_longer_exists_is_not_retried() -> None:
    error = discord.NotFound(_response(404), "gone")  # type: ignore[arg-type]

    assert await a_sender(None, error=error).send(a_draft()) is DeliveryResult.CLOSED
