"""A person's CyberWealth connected-app key: taken in a DM, never shown back, never archived.

The key is recognised by CyberWealth's own shape, taken only from a direct
message (as text or with `/connect`), stored through a port that the adapter
encrypts behind, and answered with its last four characters at most. Anything
carrying `cwk_` in a channel is never archived, never answered and never
reaches a model: its author is warned by DM instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from chatmemory.adapters.discord.bot import CyberFriendClient
from chatmemory.adapters.discord.personal_keys import channel_warning, connect_reply
from chatmemory.adapters.discord.source import is_ingestable, to_message
from chatmemory.adapters.store.personal_keys_postgres import (
    UnusableSecretsKey,
    _context,
    secrets_cipher,
)
from chatmemory.admin.oidc.crypto import UnreadableCiphertext
from chatmemory.app.language import Language
from chatmemory.app.personal_keys import (
    CYBERWEALTH,
    ConnectOutcome,
    ConnectResult,
    PersonalKeys,
    find_key,
    last_four,
    mentions_key,
)
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.privacy import HeldKey

SECRET = "a1B2-c3D4_" * 4 + "Zq9"
KEY = f"cwk_live_ABCDEFGHJK_{SECRET}"
ALICE = PersonRef("discord", 1001)
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def test_the_key_is_43_characters_after_its_prefix() -> None:
    assert len(SECRET) == 43


# --- recognising a key ---------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        KEY,
        f"minha chave do cyberwealth é {KEY}",
        f"my key: `{KEY}`",
        f"cwk_test_ABCDEFGHJK_{SECRET}",
        f"cwk_dev_ABCDEFGHJK_{SECRET}",
    ],
)
def test_a_whole_key_is_found(text: str) -> None:
    found = find_key(text)
    assert found is not None and found.endswith(SECRET)


@pytest.mark.parametrize(
    "text",
    [
        "cwk_live_ABCDEFGHJK_short",
        f"cwk_prod_ABCDEFGHJK_{SECRET}",
        f"cwk_live_abcdefghjk_{SECRET}",  # the prefix is upper-case Crockford
        f"{KEY}x",  # longer than a key
        f"{KEY} and cwk_live_ZZZZZZZZZZ_{SECRET}",  # two keys: which one?
        "no key here",
    ],
)
def test_anything_else_is_not_a_key(text: str) -> None:
    assert find_key(text) is None


@pytest.mark.parametrize("text", [KEY, "the prefix is CWK_", "cwk_live_ABC", f"x{KEY}"])
def test_anything_carrying_the_marker_mentions_a_key(text: str) -> None:
    assert mentions_key(text)


def test_ordinary_text_mentions_no_key() -> None:
    assert not mentions_key("what is my budget this month?")


def test_only_the_last_four_characters_are_shown() -> None:
    assert last_four(KEY) == KEY[-4:]


# --- connecting --------------------------------------------------------------


@dataclass
class MemoryKeyStore:
    """`PersonalKeyStore` in memory; the adapter's encryption is tested on Postgres."""

    keys: dict[tuple[PersonRef, str], str] = field(default_factory=dict)
    opted_out: set[PersonRef] = field(default_factory=set)

    async def save(self, person: PersonRef, service: str, key: str) -> HeldKey | None:
        if person in self.opted_out:
            return None
        self.keys[(person, service)] = key
        return HeldKey(service, last_four(key), NOW)

    async def key_for(self, person: PersonRef, service: str) -> str | None:
        return self.keys.get((person, service))

    async def forget(self, person: PersonRef) -> int:
        mine = [k for k in self.keys if k[0] == person]
        for k in mine:
            del self.keys[k]
        return len(mine)


async def test_a_key_in_a_dm_is_stored() -> None:
    store = MemoryKeyStore()
    outcome = await PersonalKeys(store).connect(ALICE, f"minha chave é {KEY}", direct=True)
    assert outcome.result is ConnectResult.CONNECTED
    assert outcome.held is not None and outcome.held.last4 == KEY[-4:]
    assert store.keys == {(ALICE, CYBERWEALTH): KEY}


async def test_a_key_outside_a_dm_is_not_stored() -> None:
    store = MemoryKeyStore()
    outcome = await PersonalKeys(store).connect(ALICE, KEY, direct=False)
    assert outcome.result is ConnectResult.NOT_DIRECT
    assert store.keys == {}


async def test_a_partial_key_is_not_stored() -> None:
    store = MemoryKeyStore()
    outcome = await PersonalKeys(store).connect(ALICE, "cwk_live_ABCDEF", direct=True)
    assert outcome.result is ConnectResult.MALFORMED
    assert store.keys == {}


async def test_an_opted_out_person_stores_nothing() -> None:
    store = MemoryKeyStore(opted_out={ALICE})
    outcome = await PersonalKeys(store).connect(ALICE, KEY, direct=True)
    assert outcome.result is ConnectResult.REFUSED


async def test_the_bearer_is_the_askers_own_key_and_only_for_cyberwealth() -> None:
    keys = PersonalKeys(MemoryKeyStore())
    await keys.connect(ALICE, KEY, direct=True)
    assert await keys.bearer(ALICE, CYBERWEALTH) == KEY
    assert await keys.bearer(PersonRef("discord", 2), CYBERWEALTH) is None
    assert await keys.bearer(ALICE, "issues") is None
    assert keys.covers(CYBERWEALTH) and not keys.covers("issues")


async def test_forgetting_deletes_the_key() -> None:
    keys = PersonalKeys(MemoryKeyStore())
    await keys.connect(ALICE, KEY, direct=True)
    assert await keys.forget(ALICE) == 1
    assert await keys.bearer(ALICE, CYBERWEALTH) is None


# --- what is said ----------------------------------------------------------------


@pytest.mark.parametrize("language", [Language.ENGLISH, Language.PORTUGUESE])
@pytest.mark.parametrize(
    "outcome",
    [
        ConnectOutcome(ConnectResult.CONNECTED, HeldKey(CYBERWEALTH, KEY[-4:], NOW)),
        ConnectOutcome(ConnectResult.MALFORMED),
        ConnectOutcome(ConnectResult.NOT_DIRECT),
        ConnectOutcome(ConnectResult.REFUSED),
        None,
    ],
)
def test_no_reply_repeats_the_key(outcome: ConnectOutcome | None, language: Language) -> None:
    reply = connect_reply(outcome, language)
    assert SECRET[:-4] not in reply and KEY not in reply


def test_the_connected_reply_names_the_last_four_characters() -> None:
    held = HeldKey(CYBERWEALTH, KEY[-4:], NOW)
    reply = connect_reply(ConnectOutcome(ConnectResult.CONNECTED, held), Language.PORTUGUESE)
    assert f"`{KEY[-4:]}`" in reply


def test_the_channel_warning_says_to_revoke() -> None:
    assert "revoke" in channel_warning("#general", Language.ENGLISH)
    assert "revogue" in channel_warning("#general", Language.PORTUGUESE)


# --- the secrets key --------------------------------------------------------------


def test_the_secrets_key_is_32_bytes_of_base64() -> None:
    cipher = secrets_cipher("A" * 43 + "=")
    sealed = cipher.seal(KEY, context="person_secret:1:cyberwealth")
    assert KEY.encode() not in sealed
    assert cipher.open(sealed, context="person_secret:1:cyberwealth") == KEY


def test_a_key_sealed_under_one_kind_does_not_open_under_another() -> None:
    """The kind is bound as associated data, like the person id."""
    cipher = secrets_cipher("A" * 43 + "=")
    sealed = cipher.seal(KEY, context=_context(7, CYBERWEALTH))
    assert cipher.open(sealed, context=_context(7, CYBERWEALTH)) == KEY
    with pytest.raises(UnreadableCiphertext):
        cipher.open(sealed, context=_context(7, "another-service"))
    with pytest.raises(UnreadableCiphertext):
        cipher.open(sealed, context=_context(8, CYBERWEALTH))


@pytest.mark.parametrize("raw", ["", "not base64!!", "QUJD"])
def test_an_unusable_secrets_key_is_refused(raw: str) -> None:
    with pytest.raises(UnusableSecretsKey):
        secrets_cipher(raw)


# --- the archive ---------------------------------------------------------------


@dataclass
class RawChannel:
    id: int = 500
    parent_id: int | None = None
    type: Any = None


@dataclass
class RawAuthor:
    id: int = 1001
    bot: bool = False
    global_name: str = "alice"
    display_name: str = "alice"
    name: str = "alice"


@dataclass
class RawMessage:
    content: str
    id: int = 9
    channel: RawChannel = field(default_factory=RawChannel)
    author: RawAuthor = field(default_factory=RawAuthor)
    created_at: datetime = NOW
    edited_at: datetime | None = None
    reference: Any = None
    mentions: tuple[Any, ...] = ()
    attachments: tuple[Any, ...] = ()


@pytest.mark.parametrize("content", [f"here is my key {KEY}", "try cwk_live_ABC"])
def test_a_channel_message_carrying_a_key_is_never_archived(content: str) -> None:
    raw = RawMessage(content)
    assert not is_ingestable(raw)  # type: ignore[arg-type]
    assert to_message(raw) is None  # type: ignore[arg-type]


def test_an_ordinary_channel_message_is_archived() -> None:
    raw = RawMessage("the release is on friday")
    assert is_ingestable(raw)  # type: ignore[arg-type]
    assert to_message(raw) is not None  # type: ignore[arg-type]


# --- the Discord surface -------------------------------------------------------------


@dataclass
class Asks:
    """Fails the test if anything reaches the answer path."""

    asked: list[object] = field(default_factory=list)

    async def ask(self, request: object, *_: object, **__: object) -> Any:
        self.asked.append(request)
        raise AssertionError("a message carrying a key reached the answer path")

    async def reply_language(self, person: object) -> Language:
        return Language.UNKNOWN


@dataclass
class Sent:
    replies: list[str] = field(default_factory=list)
    dms: list[str] = field(default_factory=list)


class Author:
    def __init__(self, sent: Sent) -> None:
        self.id = ALICE.platform_user_id
        self.bot = False
        self.display_name = self.name = "alice"
        self._sent = sent

    async def send(self, content: str, **_: object) -> None:
        self._sent.dms.append(content)


class Channel:
    id = 500
    name = "general"


class Message:
    def __init__(self, content: str, sent: Sent, *, guild: bool) -> None:
        self.content = content
        self.author = Author(sent)
        self.guild = object() if guild else None
        self.channel = Channel()
        self.mentions: list[object] = []
        self.attachments: list[object] = []
        self._sent = sent

    async def reply(self, content: str, **_: object) -> None:
        self._sent.replies.append(content)


def a_client(asks: Asks, keys: PersonalKeys | None) -> CyberFriendClient:
    client = CyberFriendClient(asks, 77)  # type: ignore[arg-type]
    if keys is not None:
        client.attach_personal_keys(keys)
    return client


async def test_a_key_sent_in_a_dm_is_stored_and_answered_by_its_last_four() -> None:
    asks, sent, store = Asks(), Sent(), MemoryKeyStore()
    client = a_client(asks, PersonalKeys(store))

    message: Any = Message(f"minha chave do cyberwealth é {KEY}", sent, guild=False)
    await client.on_message(message)

    assert store.keys == {(ALICE, CYBERWEALTH): KEY}
    assert asks.asked == []
    [reply] = sent.replies
    assert KEY[-4:] in reply and SECRET[:-4] not in reply
    assert "chave" in reply  # answered in the language it was sent in


async def test_a_key_in_a_dm_without_a_secrets_key_is_refused_and_never_asked() -> None:
    asks, sent = Asks(), Sent()
    client = a_client(asks, None)

    await client.on_message(Message(f"my key {KEY}", sent, guild=False))  # type: ignore[arg-type]

    assert asks.asked == []
    [reply] = sent.replies
    assert "nothing was saved" in reply and KEY not in reply


async def test_a_key_in_a_channel_is_not_stored_or_answered_and_its_author_is_warned() -> None:
    """Whether or not the bot was mentioned: the key is in front of the channel."""
    asks, sent, store = Asks(), Sent(), MemoryKeyStore()
    client = a_client(asks, PersonalKeys(store))

    await client.on_message(Message(f"here: {KEY}", sent, guild=True))  # type: ignore[arg-type]

    assert store.keys == {}
    assert asks.asked == []
    assert sent.replies == []
    [warning] = sent.dms
    assert "#general" in warning and KEY not in warning


# --- commands ----------------------------------------------------------------------


@dataclass
class ForgettingAsks(Asks):
    forgot: list[object] = field(default_factory=list)

    async def forget(self, request: object) -> Any:
        from chatmemory.ports.memory import MemoryPurge

        self.forgot.append(request)
        return MemoryPurge(turns=1)


@dataclass
class Response:
    deferred: list[bool] = field(default_factory=list)

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.deferred.append(ephemeral)


@dataclass
class Followup:
    sent: list[tuple[str, bool]] = field(default_factory=list)

    async def send(self, content: str, *, ephemeral: bool = False, **_: object) -> None:
        self.sent.append((content, ephemeral))


@dataclass
class User:
    id: int = ALICE.platform_user_id
    display_name: str = "alice"


@dataclass
class Interaction:
    guild_id: int | None
    channel_id: int | None = 500
    user: User = field(default_factory=User)
    locale: str = "en-US"
    response: Response = field(default_factory=Response)
    followup: Followup = field(default_factory=Followup)


def choice(value: str) -> Any:
    from discord import app_commands

    return app_commands.Choice(name=value, value=value)


@pytest.mark.parametrize(
    ("scope", "guild_id", "deleted"),
    [
        ("everywhere", 77, True),  # everywhere, from the server
        ("here", None, True),  # this conversation, in the DM the key was given in
        ("here", 77, False),  # one channel's conversation has nothing to do with it
    ],
)
async def test_forget_deletes_the_key_everywhere_or_in_the_dm(
    scope: str, guild_id: int | None, deleted: bool
) -> None:
    store = MemoryKeyStore()
    keys = PersonalKeys(store)
    await keys.connect(ALICE, KEY, direct=True)
    client = a_client(ForgettingAsks(), keys)
    interaction = Interaction(guild_id=guild_id)

    forget: Any = client._build_forget_command().callback
    await forget(interaction, scope=choice(scope))

    [(note, ephemeral)] = interaction.followup.sent
    assert ephemeral
    assert (store.keys == {}) is deleted
    assert ("CyberWealth key was deleted" in note) is deleted


async def test_connect_in_a_dm_stores_the_key_privately() -> None:
    store = MemoryKeyStore()
    client = a_client(Asks(), PersonalKeys(store))
    interaction = Interaction(guild_id=None, channel_id=None)

    connect: Any = client._build_connect_command().callback
    await connect(interaction, key=KEY)

    assert store.keys == {(ALICE, CYBERWEALTH): KEY}
    assert interaction.response.deferred == [True]
    [(reply, ephemeral)] = interaction.followup.sent
    assert ephemeral and KEY[-4:] in reply and KEY not in reply


async def test_a_key_given_to_ask_in_the_server_is_refused_privately_and_never_asked() -> None:
    asks, store = Asks(), MemoryKeyStore()
    client = a_client(asks, PersonalKeys(store))
    interaction = Interaction(guild_id=77)

    ask: Any = client._build_ask_command().callback
    await ask(interaction, question=f"is {KEY} ok?")

    assert asks.asked == [] and store.keys == {}
    assert interaction.response.deferred == [True]  # ephemeral from the start
    [(reply, ephemeral)] = interaction.followup.sent
    assert ephemeral and "direct message" in reply
