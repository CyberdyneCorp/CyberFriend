"""The Discord wire, faked at the two places bytes leave discord.py.

Everything above the wire is real discord.py: the guild, its roles, channels
and members are built from gateway-shaped payloads through the client's own
`ConnectionState`, a message is a `discord.Message` and a slash command is a
`discord.Interaction`. So mention parsing, DM detection, member permissions,
`message.reply` -> `channel.send` and the command tree's own dispatch all run
for real, and the bot's `setup_hook` syncs its commands exactly as it does
against Discord.

Two chokepoints are replaced:

- `FakeHTTP`, a `discord.http.HTTPClient` whose `request` answers every REST
  call the bot makes -- messages and their edits, typing, opening a DM, and
  the command sync, whose payloads are kept verbatim.
- `FakeWebhookAdapter`, for interaction responses, followups and their edits
  and deletions, which discord.py sends through its webhook adapter rather
  than `HTTPClient`.

A button press is a component interaction handed to the view discord.py
stored for the message the buttons were sent on (`FakeDiscord.press`), so
`interaction_check` and the button's callback run for real.

Every contact point with discord.py internals is in this module, and each is
named by `tests/e2e/test_harness_canary.py`, so an upgrade that moves one
fails there with a pointer rather than here with a mystery. discord.py is
pinned to ~=2.7.1 in the dev extras for the same reason.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import discord
from discord.http import HTTPClient, Route
from discord.webhook.async_ import AsyncWebhookAdapter, async_context

from chatmemory.adapters.discord.bot import CyberFriendClient

APP_ID = 424242
BOT_ID = 900001
OWNER_ID = 900002
"""The guild's owner, who is not a member here: an owner bypasses every
permission check, so no scenario's person may be one."""

EPHEMERAL = 1 << 6
IS_VOICE_MESSAGE = 1 << 13
"""The message flag Discord sets on a voice message recorded in the client."""

VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
READ_MESSAGE_HISTORY = 1 << 16
READ = VIEW_CHANNEL | READ_MESSAGE_HISTORY
EVERYONE = READ | SEND_MESSAGES

# Discord's interaction context types and installation types, from
# https://discord.com/developers/docs/interactions/application-commands
# ("Interaction Context Types": GUILD=0, BOT_DM=1, PRIVATE_CHANNEL=2;
# "Installation Context": GUILD_INSTALL=0, USER_INSTALL=1). A global command
# that omits `contexts` is usable in every context; a guild command only ever
# appears in its guild. This model of what Discord lists is hand-written, so
# the synced payload itself is also pinned by a committed snapshot.
CONTEXT_GUILD = 0
CONTEXT_BOT_DM = 1
ALL_CONTEXTS = (0, 1, 2)
GUILD_INSTALL = 0

Via = Literal["reply", "send", "dm", "response", "followup", "edit"]


@dataclass(frozen=True)
class Sent:
    """One message Discord received from the bot.

    `reply` quotes the message it answers; `dm` is a plain send to a direct
    message channel; `send` is a plain send to a guild channel; `response`
    and `followup` answer an interaction; `edit` rewrites one already sent.
    `buttons` maps each button's label to its custom id, and `disabled`
    holds the labels of the ones that cannot be pressed.
    """

    via: Via
    channel_id: int
    content: str
    ephemeral: bool = False
    message_id: int = 0
    buttons: tuple[tuple[str, str], ...] = ()
    disabled: frozenset[str] = frozenset()


def _buttons(payload: Mapping[str, Any]) -> tuple[tuple[tuple[str, str], ...], frozenset[str]]:
    """(label, custom id) for every button in a message payload, and the disabled ones."""
    found = [
        item
        for row in payload.get("components") or []
        for item in row.get("components") or []
        if item.get("type") == 2
    ]
    labels = tuple((str(b.get("label")), str(b.get("custom_id"))) for b in found)
    return labels, frozenset(str(b.get("label")) for b in found if b.get("disabled"))


class CommandNotOffered(AssertionError):
    """A slash command invoked where Discord would not have listed it."""


class CommandMentionedButNotOffered(AssertionError):
    """The bot told somebody to run a command they cannot pick from the menu there."""


class UnexpectedDiscordCall(AssertionError):
    """The bot made a REST call this wire does not model."""


_ids = itertools.count(10_000_000)


def snowflake() -> int:
    return next(_ids)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def user_payload(user_id: int, name: str, *, bot: bool = False) -> dict[str, Any]:
    return {
        "id": str(user_id),
        "username": name.lower(),
        "global_name": name,
        "discriminator": "0",
        "avatar": None,
        "bot": bot,
    }


def member_payload(user: Mapping[str, Any], role_ids: Sequence[int]) -> dict[str, Any]:
    return {
        "user": dict(user),
        "roles": [str(r) for r in role_ids],
        "joined_at": _now(),
        "deaf": False,
        "mute": False,
        "nick": None,
        "flags": 0,
    }


def _role_payload(role_id: int, name: str, permissions: int, position: int) -> dict[str, Any]:
    return {
        "id": str(role_id),
        "name": name,
        "permissions": str(permissions),
        "position": position,
        "color": 0,
        "hoist": False,
        "managed": False,
        "mentionable": False,
        "flags": 0,
    }


@dataclass
class ChannelSpec:
    """A text channel, and the roles that may read it; None is everyone."""

    id: int
    name: str
    readable_by: tuple[str, ...] | None = None


@dataclass
class GuildLayout:
    """The server a scenario runs in: its roles and its text channels."""

    guild_id: int
    roles: tuple[str, ...]
    channels: tuple[ChannelSpec, ...]
    role_ids: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.role_ids = {name: snowflake() for name in self.roles}


def _overwrites(layout: GuildLayout, spec: ChannelSpec) -> list[dict[str, Any]]:
    if spec.readable_by is None:
        return []
    everyone = {"id": str(layout.guild_id), "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)}
    allowed = [
        {"id": str(layout.role_ids[r]), "type": 0, "allow": str(READ), "deny": "0"}
        for r in spec.readable_by
    ]
    return [everyone, *allowed]


def channel_payload(layout: GuildLayout, spec: ChannelSpec, position: int) -> dict[str, Any]:
    return {
        "id": str(spec.id),
        "type": 0,
        "guild_id": str(layout.guild_id),
        "name": spec.name,
        "position": position,
        "permission_overwrites": _overwrites(layout, spec),
        "nsfw": False,
        "parent_id": None,
        "topic": None,
        "last_message_id": None,
        "rate_limit_per_user": 0,
    }


def guild_payload(layout: GuildLayout, bot_member: Mapping[str, Any]) -> dict[str, Any]:
    roles = [_role_payload(layout.guild_id, "@everyone", EVERYONE, 0)] + [
        _role_payload(role_id, name, 0, i + 1)
        for i, (name, role_id) in enumerate(layout.role_ids.items())
    ]
    return {
        "id": str(layout.guild_id),
        "name": "e2e",
        "owner_id": str(OWNER_ID),
        "roles": roles,
        "channels": [channel_payload(layout, c, i) for i, c in enumerate(layout.channels)],
        "members": [dict(bot_member)],
        "member_count": 1,
        "features": [],
        "emojis": [],
        "stickers": [],
        "threads": [],
        "voice_states": [],
        "presences": [],
        "large": False,
        "unavailable": False,
        "premium_tier": 0,
        "preferred_locale": "en-US",
        "verification_level": 0,
        "default_message_notifications": 0,
        "explicit_content_filter": 0,
        "mfa_level": 0,
        "nsfw_level": 0,
        "system_channel_flags": 0,
        "afk_timeout": 300,
        "icon": None,
    }


def message_payload(
    *,
    channel_id: int,
    author: Mapping[str, Any],
    content: str,
    guild_id: int | None = None,
    member: Mapping[str, Any] | None = None,
    mentions: Sequence[Mapping[str, Any]] = (),
    at: datetime | None = None,
    attachments: Sequence[Mapping[str, Any]] = (),
    flags: int = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(snowflake()),
        "channel_id": str(channel_id),
        "author": dict(author),
        "content": content,
        "timestamp": at.isoformat() if at is not None else _now(),
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [dict(m) for m in mentions],
        "mention_roles": [],
        "attachments": [dict(a) for a in attachments],
        "embeds": [],
        "pinned": False,
        "type": 0,
        "flags": flags,
        "components": [],
    }
    if guild_id is not None:
        payload["guild_id"] = str(guild_id)
    if member is not None:
        payload["member"] = {k: v for k, v in member.items() if k != "user"}
    return payload


def _date_id(payload: dict[str, Any], at: datetime) -> None:
    """Mint the message id from `at`: discord.py dates a message by its snowflake."""
    # The low 22 bits are free for uniqueness below the millisecond.
    payload["id"] = str(discord.utils.time_snowflake(at) + snowflake() % (1 << 22))


def attachment_payload(
    url: str,
    *,
    content_type: str = "audio/ogg",
    size: int = 48_000,
    duration: float | None = 6.5,
    filename: str = "voice-message.ogg",
) -> dict[str, Any]:
    """An attachment as the gateway sends it; by default a Discord voice note.

    `duration` is `duration_secs`, which Discord sets on voice messages only.
    """
    payload: dict[str, Any] = {
        "id": str(snowflake()),
        "filename": filename,
        "size": size,
        "url": url,
        "proxy_url": url,
        "content_type": content_type,
    }
    if duration is not None:
        payload["duration_secs"] = duration
        payload["waveform"] = "AAAA"
    return payload


class FakeHTTP(HTTPClient):
    """Discord's REST API as the bot sees it, recording what it is sent."""

    def __init__(self, loop: asyncio.AbstractEventLoop, wire: FakeDiscord) -> None:
        super().__init__(loop)
        self._wire = wire
        self.sent: list[Sent] = []
        #: Ids of messages the bot deleted, such as a progress note.
        self.deleted: list[int] = []
        self.global_commands: list[dict[str, Any]] = []
        self.guild_commands: dict[int, list[dict[str, Any]]] = {}

    async def request(self, route: Route, **kwargs: Any) -> Any:
        key = (route.method, route.path)
        if key == ("POST", "/channels/{channel_id}/messages"):
            return self._message(int(route.channel_id or 0), kwargs.get("json") or {})
        if key == ("PATCH", "/channels/{channel_id}/messages/{message_id}"):
            return self._edit(int(route.channel_id or 0), route, kwargs.get("json") or {})
        if key == ("DELETE", "/channels/{channel_id}/messages/{message_id}"):
            self.deleted.append(int(route.url.rsplit("/", 1)[1]))
            return None
        if key == ("POST", "/channels/{channel_id}/typing"):
            return None
        if key == ("POST", "/users/@me/channels"):
            return self._wire.dm_payload(int(kwargs["json"]["recipient_id"]))
        if key == ("GET", "/users/{user_id}"):
            # `client.fetch_user`, which the scheduled-task and alert
            # messengers call before opening a DM.
            # A route keeps no `user_id` attribute, only the formatted URL.
            return self._wire.user(int(route.url.rsplit("/", 1)[1]))
        if key == ("PUT", "/applications/{application_id}/commands"):
            self.global_commands = list(kwargs["json"])
            return _registered(self.global_commands)
        if key == ("PUT", "/applications/{application_id}/guilds/{guild_id}/commands"):
            self.guild_commands[int(route.guild_id or 0)] = list(kwargs["json"])
            return _registered(self.guild_commands[int(route.guild_id or 0)], route.guild_id)
        raise UnexpectedDiscordCall(f"{route.method} {route.path}")

    def _message(self, channel_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        content = str(payload.get("content") or "")
        if payload.get("message_reference"):
            via: Via = "reply"
        elif self._wire.is_dm(channel_id):
            via = "dm"
        else:
            via = "send"
        message = self._wire.bot_message(channel_id, content)
        buttons, disabled = _buttons(payload)
        self.sent.append(
            Sent(via, channel_id, content, False, int(message["id"]), buttons, disabled)
        )
        return message

    def _edit(self, channel_id: int, route: Route, payload: Mapping[str, Any]) -> dict[str, Any]:
        """A message the bot rewrote: an expired prompt, for one."""
        message_id = int(route.url.rsplit("/", 1)[1])
        content = str(payload.get("content") or "")
        buttons, disabled = _buttons(payload)
        self.sent.append(Sent("edit", channel_id, content, False, message_id, buttons, disabled))
        return {**self._wire.bot_message(channel_id, content), "id": str(message_id)}


def _registered(payload: Sequence[Mapping[str, Any]], guild_id: object = None) -> list[Any]:
    """What Discord answers a bulk upsert with: the commands, with ids."""
    out = []
    for command in payload:
        entry = {
            **command,
            "id": str(snowflake()),
            "application_id": str(APP_ID),
            "version": "1",
            "default_member_permissions": command.get("default_member_permissions"),
        }
        if guild_id is not None:
            entry["guild_id"] = str(guild_id)
        out.append(entry)
    return out


class FakeWebhookAdapter(AsyncWebhookAdapter):
    """Interaction responses and followups, recorded in the order they happen.

    `events` keeps the response type too, so a scenario can check that a
    command deferred before it followed up.
    """

    def __init__(self, wire: FakeDiscord) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self._wire = wire
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def request(self, route: Route, session: Any, **kwargs: Any) -> Any:
        payload: dict[str, Any] = kwargs.get("payload") or {}
        token = str(route.webhook_token)
        channel_id = self._wire.interaction_channel(token)
        if route.path.endswith("/callback"):
            self.events.append(("response", payload))
            data = payload.get("data") or {}
            if data.get("content"):
                self._record("response", channel_id, data)
            return {"interaction": {"id": str(route.webhook_id), "type": 2}}
        if route.method == "DELETE":
            # `delete_original_response`: the deferred "thinking" message.
            self.events.append(("delete", payload))
            return None
        if route.method == "PATCH":
            self.events.append(("edit", payload))
            self._record("edit", channel_id, payload)
            return self._wire.bot_message(channel_id, str(payload.get("content") or ""))
        self.events.append(("followup", payload))
        message = self._wire.bot_message(channel_id, str(payload.get("content") or ""))
        self._record("followup", channel_id, payload, int(message["id"]))
        return message

    def _record(
        self, via: Via, channel_id: int, data: Mapping[str, Any], message_id: int = 0
    ) -> None:
        ephemeral = bool(int(data.get("flags") or 0) & EPHEMERAL)
        buttons, disabled = _buttons(data)
        self._wire.http.sent.append(
            Sent(
                via,
                channel_id,
                str(data.get("content") or ""),
                ephemeral,
                message_id,
                buttons,
                disabled,
            )
        )


class FakeDiscord:
    """One guild, the bot's client inside it, and the wire it talks over."""

    def __init__(self, client: CyberFriendClient, layout: GuildLayout) -> None:
        self.client = client
        self.layout = layout
        self.bot_user = user_payload(BOT_ID, "CyberFriend", bot=True)
        self._users: dict[int, dict[str, Any]] = {BOT_ID: self.bot_user}
        self._dm_channels: dict[int, int] = {}
        self._interactions: dict[str, int] = {}
        self.http: FakeHTTP
        self.webhooks = FakeWebhookAdapter(self)
        self.guild: discord.Guild

    @property
    def state(self) -> Any:
        return self.client._connection

    async def start(self) -> None:
        """Log in without a network, load the guild, and run the real setup_hook."""
        self.http = FakeHTTP(asyncio.get_running_loop(), self)
        # Three holders of the one client: the Client, its ConnectionState,
        # and the command tree, which captured it when it was built.
        self.client.http = self.http
        self.state.http = self.http
        self.client.tree._http = self.http
        await self.client._async_setup_hook()
        self.state.user = discord.ClientUser(state=self.state, data=self.bot_user)  # type: ignore[arg-type]
        self.state.application_id = APP_ID
        bot_member = member_payload(self.bot_user, [])
        self.guild = self.state._add_guild_from_data(guild_payload(self.layout, bot_member))
        # Errors from a command callback are logged and swallowed by the
        # tree's default handler; a scenario must see them instead.
        self.client.tree.on_error = _reraise  # type: ignore[method-assign]
        await self.client.setup_hook()

    # --- people and channels -------------------------------------------

    def add_member(self, name: str, *, roles: Sequence[str] = ()) -> discord.Member:
        user = user_payload(snowflake(), name)
        self._users[int(user["id"])] = user
        data = member_payload(user, [self.layout.role_ids[r] for r in roles])
        member = discord.Member(data=data, guild=self.guild, state=self.state)  # type: ignore[arg-type]
        self.guild._add_member(member)
        return member

    def channel(self, name: str) -> discord.TextChannel:
        found = discord.utils.get(self.guild.text_channels, name=name)
        assert found is not None, f"no channel #{name} in the layout"
        return found

    def user(self, user_id: int) -> dict[str, Any]:
        """A member's user payload, as `GET /users/{id}` answers it."""
        return dict(self._users[user_id])

    def dm_channel_id(self, user_id: int) -> int:
        return self._dm_channels.setdefault(user_id, snowflake())

    def dm_payload(self, user_id: int) -> dict[str, Any]:
        return {
            "id": str(self.dm_channel_id(user_id)),
            "type": 1,
            "recipients": [self._users[user_id]],
            "last_message_id": None,
        }

    def dm_channel(self, user_id: int) -> discord.DMChannel:
        """The DM channel with `user_id`, registered as the gateway would."""
        existing = self.state._get_private_channel(self.dm_channel_id(user_id))
        if existing is not None:
            return existing  # type: ignore[no-any-return]
        return self.state.add_dm_channel(self.dm_payload(user_id))  # type: ignore[no-any-return]

    def private_thread(self, parent: str) -> discord.Thread:
        """A private thread under #`parent`: readable only by who was added to it."""
        data = {
            "id": str(snowflake()),
            "guild_id": str(self.layout.guild_id),
            "parent_id": str(self.channel(parent).id),
            "owner_id": str(OWNER_ID),
            "name": "private",
            "type": 12,
            "last_message_id": None,
            "rate_limit_per_user": 0,
            "message_count": 0,
            "member_count": 0,
            "thread_metadata": {
                "archived": False,
                "auto_archive_duration": 1440,
                "archive_timestamp": _now(),
                "locked": False,
                "invitable": False,
            },
        }
        return discord.Thread(guild=self.guild, state=self.state, data=data)  # type: ignore[arg-type]

    def is_dm(self, channel_id: int) -> bool:
        return channel_id in self._dm_channels.values()

    def bot_message(self, channel_id: int, content: str) -> dict[str, Any]:
        guild = None if self.is_dm(channel_id) else self.layout.guild_id
        return message_payload(
            channel_id=channel_id, author=self.bot_user, content=content, guild_id=guild
        )

    # --- inbound -------------------------------------------------------

    def dm_message(
        self,
        member: discord.Member,
        content: str,
        *,
        attachments: Sequence[Mapping[str, Any]] = (),
        flags: int = 0,
        at: datetime | None = None,
    ) -> discord.Message:
        """A message in the member's DM with the bot, with any attachments.

        `at` dates it, as `chatter` does; without one it dates to 2015."""
        channel = self.dm_channel(member.id)
        data = message_payload(
            channel_id=channel.id,
            author=self._users[member.id],
            content=content,
            attachments=attachments,
            flags=flags,
            at=at,
        )
        if at is not None:
            _date_id(data, at)
        return discord.Message(state=self.state, channel=channel, data=data)  # type: ignore[arg-type]

    def channel_message(
        self,
        member: discord.Member,
        channel: discord.TextChannel,
        content: str,
        *,
        attachments: Sequence[Mapping[str, Any]] = (),
        flags: int = 0,
    ) -> discord.Message:
        """A message in a guild channel that mentions the bot, with any attachments."""
        user = self._users[member.id]
        data = message_payload(
            channel_id=channel.id,
            author=user,
            content=f"<@{BOT_ID}> {content}".rstrip(),
            guild_id=self.layout.guild_id,
            member=member_payload(user, [r.id for r in member.roles[1:]]),
            mentions=[{**self.bot_user, "member": member_payload(self.bot_user, [])}],
            attachments=attachments,
            flags=flags,
        )
        return discord.Message(state=self.state, channel=channel, data=data)  # type: ignore[arg-type]

    def chatter(
        self,
        member: discord.Member,
        channel: discord.TextChannel | discord.Thread,
        content: str,
        *,
        mentions: Sequence[discord.Member] = (),
        at: datetime,
        attachments: Sequence[Mapping[str, Any]] = (),
        flags: int = 0,
    ) -> discord.Message:
        """A message in a guild channel that is not addressed to the bot.

        What ingest captures rather than what the bot answers. `at` is when it
        was said, so a scenario on the fake clock can place it in a period.
        Required, because discord.py dates a message by its snowflake rather
        than its timestamp: the id is minted from `at`, and a counter id would
        date it to 2015.
        """
        user = self._users[member.id]
        data = message_payload(
            channel_id=channel.id,
            author=user,
            content=content,
            guild_id=self.layout.guild_id,
            member=member_payload(user, [r.id for r in member.roles[1:]]),
            mentions=[self._mention(m) for m in mentions],
            at=at,
            attachments=attachments,
            flags=flags,
        )
        _date_id(data, at)
        return discord.Message(state=self.state, channel=channel, data=data)  # type: ignore[arg-type]

    def _mention(self, member: discord.Member) -> dict[str, Any]:
        user = self._users[member.id]
        return {**user, "member": member_payload(user, [r.id for r in member.roles[1:]])}

    # --- commands ------------------------------------------------------

    def offered(self, *, dm: bool) -> frozenset[str]:
        """The command names Discord would list in a DM with the bot, or in the guild."""
        context = CONTEXT_BOT_DM if dm else CONTEXT_GUILD
        names = {
            c["name"]
            for c in self.http.global_commands
            if context in (c.get("contexts") or ALL_CONTEXTS)
            and GUILD_INSTALL in (c.get("integration_types") or (GUILD_INSTALL,))
        }
        if not dm:
            names |= {c["name"] for c in self.http.guild_commands.get(self.layout.guild_id, [])}
        return frozenset(names)

    def _definition(self, name: str, dm: bool) -> Mapping[str, Any]:
        pool = list(self.http.global_commands)
        if not dm:
            pool += self.http.guild_commands.get(self.layout.guild_id, [])
        return next(c for c in pool if c["name"] == name)

    def interaction_channel(self, token: str) -> int:
        return self._interactions[token]

    def interaction(
        self,
        member: discord.Member,
        name: str,
        options: Mapping[str, object],
        *,
        channel: discord.TextChannel | None,
        locale: str = "en-US",
    ) -> discord.Interaction[Any]:
        """A slash-command interaction, refused if Discord would not offer it there."""
        dm = channel is None
        top, *sub = name.split()
        if top not in self.offered(dm=dm):
            where = "a DM" if dm else "the guild"
            raise CommandNotOffered(f"/{top} is not offered in {where}")
        definition = self._definition(top, dm)
        data = {
            "id": str(snowflake()),
            "name": top,
            "type": 1,
            "options": _options(definition, sub, options),
        }
        payload = self._interaction_payload(member, data, channel, locale=locale)
        return discord.Interaction(data=payload, state=self.state)  # type: ignore[arg-type]

    async def press(self, member: discord.Member, sent: Sent, label: str) -> None:
        """`member` presses the button labelled `label` on the message `sent`.

        Handed to the view discord.py stored for that message, as the gateway
        would hand it, and awaited: `interaction_check` and the callback run
        for real. A press on a message whose view has stopped reaches nothing,
        as on Discord.
        """
        custom_id = dict(sent.buttons)[label]
        channel = None if self.is_dm(sent.channel_id) else self.guild.get_channel(sent.channel_id)
        data = {"custom_id": custom_id, "component_type": 2}
        payload = self._interaction_payload(
            member,
            data,
            channel,  # type: ignore[arg-type]
            kind=3,
        )
        payload["message"] = {
            **self.bot_message(sent.channel_id, sent.content),
            "id": str(sent.message_id),
        }
        interaction = discord.Interaction(data=payload, state=self.state)  # type: ignore[arg-type]
        item = self.state._view_store._views.get(sent.message_id, {}).get((2, custom_id))
        if item is None or item.view is None:
            return
        await item.view._scheduled_task(item, interaction)

    def _interaction_payload(
        self,
        member: discord.Member,
        data: Mapping[str, Any],
        channel: discord.TextChannel | None,
        *,
        kind: int = 2,
        locale: str = "en-US",
    ) -> dict[str, Any]:
        user = self._users[member.id]
        interaction_id = snowflake()
        token = f"token-{interaction_id}"
        payload: dict[str, Any] = {
            "id": str(interaction_id),
            "application_id": str(APP_ID),
            "type": kind,
            "token": token,
            "version": 1,
            "attachment_size_limit": 8_000_000,
            "locale": locale,
            "entitlements": [],
            "authorizing_integration_owners": {"0": str(self.layout.guild_id)},
            "app_permissions": "0",
            "data": dict(data),
        }
        if channel is None:
            dm = self.dm_channel(member.id)
            payload |= {"context": CONTEXT_BOT_DM, "user": user, "channel_id": str(dm.id)}
            payload["channel"] = self.dm_payload(member.id)
            self._interactions[token] = dm.id
        else:
            payload |= {
                "context": CONTEXT_GUILD,
                "guild_id": str(self.layout.guild_id),
                "guild_locale": "en-US",
                "member": member_payload(user, [r.id for r in member.roles[1:]]),
                "channel_id": str(channel.id),
                "channel": {
                    "id": str(channel.id),
                    "type": 0,
                    "guild_id": str(self.layout.guild_id),
                },
            }
            self._interactions[token] = channel.id
        return payload

    @contextmanager
    def webhooks_installed(self) -> Iterator[None]:
        """Route interaction responses to the fake adapter for the duration."""
        token = async_context.set(self.webhooks)
        try:
            yield
        finally:
            async_context.reset(token)


def _option_type(definition: Mapping[str, Any], name: str) -> int:
    for option in definition.get("options") or []:
        if option["name"] == name:
            return int(option["type"])
    raise CommandNotOffered(f"/{definition['name']} has no option {name!r}")


def _options(
    definition: Mapping[str, Any], sub: Sequence[str], options: Mapping[str, object]
) -> list[dict[str, Any]]:
    """The interaction's option tree, typed from the synced definition."""
    if sub:
        [child] = [o for o in definition.get("options") or [] if o["name"] == sub[0]]
        return [{"name": sub[0], "type": child["type"], "options": _options(child, (), options)}]
    return [
        {"name": k, "type": _option_type(definition, k), "value": v} for k, v in options.items()
    ]


async def _reraise(interaction: discord.Interaction[Any], error: Exception) -> None:
    raise error
