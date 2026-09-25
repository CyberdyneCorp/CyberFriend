"""The formatter is on the path a real reply takes, not merely importable.

This project's recurring defect is a capability that is built, tested and
wired to nothing, so a real `CyberFriendClient` answers here -- by mention and
by slash command -- and the assertions are about what reached the send calls:
the split messages, the unmasked link, and `AllowedMentions.none()` on every
one of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import discord

from chatmemory.adapters.discord.bot import CyberFriendClient
from chatmemory.adapters.discord.formatting import DISCORD_MESSAGE_LIMIT, FENCE
from chatmemory.app.disclosure import ScopedAnswer
from chatmemory.app.reasoning.stages import SYNTHESIS_SYSTEM
from chatmemory.domain.identity import ChannelRef
from chatmemory.ports.answers import Answer, Citation

GUILD_ID = 77
CHANNEL_ID = 500
ALICE_ID = 1001
CITED = "https://discord.com/channels/77/500/9"

LONG_WITH_CODE = (
    "@everyone here is the fix, see [the docs](https://evil.example/docs).\n\n"
    + "\n\n".join("Context " + "words " * 80 for _ in range(4))
    + "\n\n```python\n"
    + "\n".join(f"step_{i}()" for i in range(60))
    + "\n```\n\n"
    + "\n\n".join("Aftermath " + "words " * 80 for _ in range(3))
)


@dataclass
class Outcome:
    scoped: ScopedAnswer
    rate_limited: bool = False
    retry_after_seconds: float = 0.0
    alert: None = None
    suggestion: None = None


@dataclass
class ScriptedAsks:
    text: str
    calls: int = 0

    async def ask(self, request: object, confirm: object = None, **_: object) -> Outcome:
        self.calls += 1
        citation = Citation(ChannelRef("discord", CHANNEL_ID), 9, "sam", "the fix", CITED)
        return Outcome(ScopedAnswer(Answer(self.text, (citation,)), frozenset()))


@dataclass
class Sink:
    sent: list[dict[str, Any]] = field(default_factory=list)

    async def record(self, content: str, **kwargs: Any) -> None:
        self.sent.append({"content": content, **kwargs})


class Typing:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class Channel:
    def __init__(self, sink: Sink) -> None:
        self.id = CHANNEL_ID
        self.name = "general"
        self.send = sink.record

    def typing(self) -> Typing:
        return Typing()


class Author:
    def __init__(self) -> None:
        self.id = ALICE_ID
        self.bot = False
        self.display_name = "alice"
        self.name = "alice"


class Message:
    def __init__(self, sink: Sink, bot_user: object) -> None:
        self.author = Author()
        self.guild = object()
        self.content = f"<@{getattr(bot_user, 'id', 0)}> how do I fix it?"
        self.mentions = [bot_user]
        self.channel = Channel(sink)
        self.reply = sink.record


class BotUser:
    id = 4242


def assert_every_send_is_safe(sent: list[dict[str, Any]]) -> None:
    assert len(sent) > 1, "the long answer should have been split"
    for call in sent:
        mentions = call.get("allowed_mentions")
        assert isinstance(mentions, discord.AllowedMentions)
        assert mentions.everyone is False
        assert mentions.users is False
        assert mentions.roles is False
        assert len(call["content"]) <= DISCORD_MESSAGE_LIMIT
        assert call["content"].count(FENCE) % 2 == 0
    text = "\n".join(call["content"] for call in sent)
    assert "@everyone" not in text
    assert "[the docs](https://evil.example" not in text
    assert "https://evil.example/docs" in text
    assert f"[sam]({CITED})" in text


def a_client(asks: ScriptedAsks) -> CyberFriendClient:
    client = CyberFriendClient(asks, GUILD_ID)  # type: ignore[arg-type]
    client._connection.user = BotUser()  # type: ignore[assignment]
    return client


async def test_a_mention_reply_is_formatted_split_and_notifies_no_one() -> None:
    asks = ScriptedAsks(LONG_WITH_CODE)
    client = a_client(asks)
    sink = Sink()

    await client.on_message(Message(sink, client.user))  # type: ignore[arg-type]

    assert asks.calls == 1
    assert_every_send_is_safe(sink.sent)
    # The first part is the reply to the asker, without pinging them.
    assert sink.sent[0].get("mention_author") is False


class Response:
    async def defer(self, **kwargs: Any) -> None:
        return None


class Followup:
    def __init__(self, sink: Sink) -> None:
        self.send = sink.record


class Interaction:
    def __init__(self, sink: Sink) -> None:
        self.user = Author()
        self.guild_id = GUILD_ID
        self.channel_id = CHANNEL_ID
        self.channel = Channel(sink)
        self.response = Response()
        self.followup = Followup(sink)


async def test_a_slash_command_answer_is_formatted_split_and_notifies_no_one() -> None:
    asks = ScriptedAsks(LONG_WITH_CODE)
    client = a_client(asks)
    sink = Sink()

    command = client._build_ask_command()
    await command.callback(Interaction(sink), "how do I fix it?")  # type: ignore[arg-type,call-arg]

    assert asks.calls == 1
    assert_every_send_is_safe(sink.sent)


def test_the_synthesiser_is_asked_for_discord_markdown_without_links() -> None:
    assert "Discord markdown" in SYNTHESIS_SYSTEM
    assert '"-# "' in SYNTHESIS_SYSTEM
    assert "fenced code block" in SYNTHESIS_SYSTEM
    assert "Do not write links" in SYNTHESIS_SYSTEM
