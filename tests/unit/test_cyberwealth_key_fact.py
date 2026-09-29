"""The CyberWealth connected-app key, listed, shown and forgotten like a personal fact.

A key sent in a DM is already stored sealed (`test_personal_keys`). These
tests cover what makes it behave like the other facts: "what do you know
about me?" lists it by its last four characters in a DM and never mentions it
in a channel, "what's my CyberWealth key?" answers the same way, and "forget
my CyberWealth key" deletes it alone. Showing it never opens the sealed key.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from chatmemory.app.ask import AskService
from chatmemory.app.fact_replies import key_forgotten_reply, key_line, key_shown_reply
from chatmemory.app.facts import PersonalFactsService
from chatmemory.app.language import Language
from chatmemory.app.personal_keys import CYBERWEALTH, PersonalKeys, last_four
from chatmemory.app.routing import FactAction, fact_intent
from chatmemory.composition import build_ask_service
from chatmemory.config import Settings
from chatmemory.domain.identity import PersonRef
from chatmemory.entrypoints.bot import build_bot
from chatmemory.ports.facts import FactKind
from chatmemory.ports.privacy import HeldKey
from tests.unit.test_facts import FakeFactStore
from tests.unit.test_facts_behaviour import (
    BASE,
    LEO,
    RecordingAnswers,
    build,
    in_channel,
    in_dm,
    reply,
)
from tests.unit.test_personal_keys import NOW

EN, PT = Language.ENGLISH, Language.PORTUGUESE
KEY = f"cwk_live_ABCDEFGHJK_{'x' * 39}Nd4k"
NEW_KEY = f"cwk_live_ABCDEFGHJK_{'y' * 39}Zq9w"


@dataclass
class SealedKeyStore:
    """A key store whose sealed keys cannot be opened: showing one must not try."""

    keys: dict[tuple[PersonRef, str], str] = field(default_factory=dict)
    opened: int = 0

    async def save(self, person: PersonRef, service: str, key: str) -> HeldKey | None:
        self.keys[(person, service)] = key
        return HeldKey(service, last_four(key), NOW)

    async def key_for(self, person: PersonRef, service: str) -> str | None:
        self.opened += 1
        raise AssertionError("the plaintext key was read to answer a fact request")

    async def held(self, person: PersonRef, service: str) -> HeldKey | None:
        key = self.keys.get((person, service))
        return HeldKey(service, last_four(key), NOW) if key is not None else None

    async def forget(self, person: PersonRef, service: str | None = None) -> int:
        mine = [k for k in self.keys if k[0] == person and service in (None, k[1])]
        for k in mine:
            del self.keys[k]
        return len(mine)


def keyed(*, with_key: bool = True) -> tuple[AskService, FakeFactStore, SealedKeyStore]:
    service, facts, _ = build()
    store = SealedKeyStore()
    if with_key:
        store.keys[(LEO, CYBERWEALTH)] = KEY
    service.attach_personal_keys(PersonalKeys(store))
    return service, facts, store


# --- recognising the request -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("esqueça minha chave do cyberwealth", FactAction.FORGET_KEY),
        ("apague minha chave do cyberwealth", FactAction.FORGET_KEY),
        ("Esquece a minha chave do CyberWealth, por favor", FactAction.FORGET_KEY),
        ("forget my cyberwealth key", FactAction.FORGET_KEY),
        ("delete my CyberWealth API key", FactAction.FORGET_KEY),
        ("qual é a minha chave do cyberwealth?", FactAction.SHOW_KEY),
        ("qual a minha chave do CyberWealth", FactAction.SHOW_KEY),
        ("você tem minha chave do cyberwealth?", FactAction.SHOW_KEY),
        ("what's my cyberwealth key?", FactAction.SHOW_KEY),
        ("do you have my CyberWealth key?", FactAction.SHOW_KEY),
        ("o que você sabe sobre mim?", FactAction.SHOW),
        ("what do you know about me?", FactAction.SHOW),
    ],
)
def test_key_requests_are_recognised_in_both_languages(text: str, action: FactAction) -> None:
    intent = fact_intent(text)
    assert intent is not None
    assert (intent.action, intent.kind) == (action, None)


@pytest.mark.parametrize(
    ("text", "kind"),
    [("esqueça meu email", FactKind.EMAIL), ("forget my wallet", FactKind.ETH_WALLET)],
)
def test_forgetting_a_fact_is_not_forgetting_the_key(text: str, kind: FactKind) -> None:
    intent = fact_intent(text)
    assert intent is not None and (intent.action, intent.kind) == (FactAction.FORGET, kind)


@pytest.mark.parametrize(
    "text",
    [
        "what is my cyberwealth portfolio?",
        "minha chave do cyberwealth expirou",
        "how do I create a cyberwealth key?",
    ],
)
def test_other_mentions_of_the_key_are_ordinary_questions(text: str) -> None:
    assert fact_intent(text) is None


# --- the replies -------------------------------------------------------------------


def test_the_key_is_shown_by_its_last_four_characters_only() -> None:
    held = HeldKey(CYBERWEALTH, last_four(KEY), NOW)
    assert key_line(held, EN) == "• CyberWealth key: `…Nd4k`"
    assert key_line(held, PT) == "• Chave do CyberWealth: `…Nd4k`"
    for language in (EN, PT):
        shown = key_shown_reply(held, True, language)
        assert "Nd4k" in shown and KEY[:-4] not in shown and "cwk_" not in shown


@pytest.mark.parametrize("language", [EN, PT])
def test_in_a_channel_the_reply_is_the_same_whether_or_not_a_key_is_held(
    language: Language,
) -> None:
    held = HeldKey(CYBERWEALTH, "Nd4k", NOW)
    assert key_shown_reply(held, False, language) == key_shown_reply(None, False, language)
    assert "Nd4k" not in key_shown_reply(held, False, language)


def test_the_forgotten_reply_is_in_the_askers_language() -> None:
    assert key_forgotten_reply(EN) == "Done. I don't have a CyberWealth key for you any more."
    assert key_forgotten_reply(PT) == "Pronto. Não tenho mais sua chave do CyberWealth."


# --- through the ask service ----------------------------------------------------------


async def test_the_dm_listing_includes_the_key_masked() -> None:
    service, _, store = keyed()
    await service.ask(in_dm("call me Leo"))

    shown = await reply(service, in_dm("o que você sabe sobre mim?"))

    assert "Chave do CyberWealth: `…Nd4k`" in shown
    assert "Leo" in shown
    assert KEY[:-4] not in shown
    assert store.opened == 0


async def test_the_dm_listing_shows_a_key_held_with_no_facts() -> None:
    service, _, _ = keyed()
    shown = await reply(service, in_dm("what do you know about me?"))
    assert shown.startswith("Here's what you've asked me to remember:")
    assert "CyberWealth key: `…Nd4k`" in shown


async def test_a_channel_listing_never_mentions_the_key() -> None:
    service, _, store = keyed()
    await service.ask(in_dm("call me Leo"))

    for text in ("o que você sabe sobre mim?", "what do you know about me?"):
        shown = await reply(service, in_channel(text))
        assert "Nd4k" not in shown
        assert "cyberwealth" not in shown.casefold() and "chave" not in shown.casefold()
    assert store.opened == 0


async def test_asking_for_the_key_answers_masked_in_a_dm_and_says_none_otherwise() -> None:
    service, _, store = keyed()
    assert await reply(service, in_dm("qual é a minha chave do cyberwealth?")) == (
        "• Chave do CyberWealth: `…Nd4k`"
    )
    empty, _, _ = keyed(with_key=False)
    missing = await reply(empty, in_dm("what's my cyberwealth key?"))
    assert missing.startswith("I don't have a CyberWealth key saved for you.")
    assert store.opened == 0


async def test_asking_for_the_key_in_a_channel_gives_the_direct_only_reply() -> None:
    service, _, _ = keyed()
    empty, _, _ = keyed(with_key=False)
    held = await reply(service, in_channel("what's my cyberwealth key?"))
    none = await reply(empty, in_channel("what's my cyberwealth key?"))
    assert held == none == "I only show your CyberWealth key in a direct message. Ask me there."


async def test_forgetting_the_key_deletes_it_and_no_fact() -> None:
    service, facts, store = keyed()
    await service.ask(in_dm("my email is leo@example.com"))

    done = await reply(service, in_dm("esqueça minha chave do cyberwealth"))

    assert done == "Pronto. Não tenho mais sua chave do CyberWealth."
    assert store.keys == {}
    assert facts.rows == {(LEO, FactKind.EMAIL): "leo@example.com"}


async def test_forgetting_a_fact_keeps_the_key() -> None:
    service, _, store = keyed()
    await service.ask(in_dm("my email is leo@example.com"))
    await service.ask(in_dm("forget my email"))
    assert (LEO, CYBERWEALTH) in store.keys


async def test_forgetting_everything_you_know_includes_the_key() -> None:
    service, facts, store = keyed()
    await service.ask(in_dm("my email is leo@example.com"))
    await service.ask(in_dm("forget everything you know about me"))
    assert store.keys == {} and facts.rows == {}


async def test_a_new_key_replaces_the_old_one_in_the_listing() -> None:
    service, _, store = keyed()
    keys = PersonalKeys(store)
    await keys.connect(LEO, f"minha chave do cyberwealth é {NEW_KEY}", direct=True)

    shown = await reply(service, in_dm("o que você sabe sobre mim?"))

    assert "`…Zq9w`" in shown and "Nd4k" not in shown
    assert store.keys == {(LEO, CYBERWEALTH): NEW_KEY}


async def test_without_a_key_store_the_requests_are_still_answered() -> None:
    service, _, _ = build()
    assert (await reply(service, in_dm("forget my cyberwealth key"))).startswith("Done.")
    assert (await reply(service, in_dm("what's my cyberwealth key?"))).startswith(
        "I don't have a CyberWealth key"
    )


# --- wiring ---------------------------------------------------------------------------


async def test_the_built_bot_lists_the_key() -> None:
    """Behavioural, through `build_bot`: the process attaches the key store to
    the ask service as well as to the Discord client."""
    store = SealedKeyStore({(LEO, CYBERWEALTH): KEY})
    graph = build_bot(
        Settings(**BASE),  # type: ignore[arg-type]
        RecordingAnswers(),  # type: ignore[arg-type]
        facts=PersonalFactsService(FakeFactStore()),
        personal_keys=PersonalKeys(store),
    )
    shown = await reply(graph.asks, in_dm("what do you know about me?"))
    assert "CyberWealth key: `…Nd4k`" in shown


def test_a_composed_ask_service_holds_no_key_until_one_is_attached() -> None:
    from chatmemory.adapters.discord.acl import static_guild
    from tests.unit.fakes import FakeGuild

    asks = build_ask_service(
        Settings(**BASE),  # type: ignore[arg-type]
        static_guild(FakeGuild()),
        object(),  # type: ignore[arg-type]
    )
    assert asks._keys is None
