"""The production DM, replayed: facts said loosely, and the wallet just given.

    Guarde na sua memoria que eu quero os meus valores em reais brasileiro
    Oque vide sabe sobre mim ?
    Você sabe qual é a moeda do meu país ?
    verifique as pools de liquidez nesta wallet 0xB26B…45e0
    Porque você não respondeu o valor ... na minha moeda de base?  -> "Qual carteira?"
    A carteira que eu acabei de passar                              -> corpus search

Each fact line went to the corpus; the follow-ups asked for an address the
asker had typed two turns earlier. The address a follow-up may use is the
asker's own, typed in the same conversation: never somebody else's message,
never retrieved content, never another location's.
"""

from __future__ import annotations

from datetime import timedelta

from tests.e2e.conftest import COLLEAGUE_CRYPTO
from tests.e2e.harness.conversation import E2EBot
from tests.e2e.harness.process import NOW

ADDRESS = "0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0"
COLLEAGUES_ADDRESS = "0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5"
ANAS_ADDRESS = "0x" + "a1" * 20

POOLS = f"verifique as pools de liquidez nesta wallet {ADDRESS}"
WHY_NOT_IN_MY_CURRENCY = (
    "Porque você não respondeu o valor das pools na minha moeda de base?"
)
JUST_GAVE = "A carteira que eu acabei de passar"


def _reached(bot: E2EBot, since: int, address: str) -> bool:
    """Whether a chain read after `since` was about `address`."""
    needle = address.lower()[2:]
    explorer = any(
        r.url.params.get("holder_address_hash", "").lower() == address.lower()
        for r in bot.web.calls[since:]
    )
    return explorer or any(needle in str(c).lower() for c in bot.web.rpc_calls(since))


def _no_foreign_reads(bot: E2EBot, since: int) -> None:
    for address in (COLLEAGUES_ADDRESS, ANAS_ADDRESS):
        assert not _reached(bot, since, address), f"read {address}, which the asker never typed"


# --- the facts -------------------------------------------------------------------


async def test_a_currency_kept_in_memory_is_saved_and_shown(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    dm = bot.dm(leo)

    saved = await dm.say(
        "Guarde na sua memoria que eu quero os meus valores em reais brasileiro"
    )

    assert saved.edge() == "NONE" and not saved.searched
    assert "também em **real brasileiro (BRL)**" in saved.text
    assert (await bot.facts_of(leo))["preferred_currency"] == "BRL"

    shown = await dm.say("Oque vide sabe sobre mim ?")

    assert not shown.searched
    assert "• Moeda preferida: **real brasileiro (BRL)**" in shown.text
    shown.assert_language("pt")

    currency = await dm.say("Você sabe qual é a moeda do meu país ?")

    assert not currency.searched
    assert "Moeda preferida: **real brasileiro (BRL)**" in currency.text


async def test_the_currency_asked_for_with_none_saved_says_how_to_save_it(bot: E2EBot) -> None:
    leo = bot.person("Leo")

    turn = await bot.dm(leo).say("Você sabe qual é a moeda do meu país ?")

    assert turn.edge() == "NONE" and not turn.searched
    assert "Não tenho uma moeda preferida salva para você" in turn.text
    assert "`minha moeda é o real`" in turn.text
    turn.assert_language("pt")


# --- the wallet just given --------------------------------------------------------


async def test_in_a_dm_the_follow_ups_read_the_wallet_the_asker_just_typed(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    first = await dm.say(POOLS)
    assert first.edge() == "CHAIN" and _reached(bot, 0, ADDRESS)

    since = len(bot.web.calls)
    why = await dm.say(WHY_NOT_IN_MY_CURRENCY)

    assert why.edge() == "CHAIN" and not why.searched
    assert _reached(bot, since, ADDRESS), "asked 'which wallet?' about the one just given"
    assert "Qual carteira" not in why.text and "Which wallet" not in why.text
    _no_foreign_reads(bot, since)

    since = len(bot.web.calls)
    again = await dm.say(JUST_GAVE)

    assert again.edge() == "CHAIN" and not again.searched, "the follow-up searched the corpus"
    assert _reached(bot, since, ADDRESS)
    assert COLLEAGUE_CRYPTO not in again.text
    _no_foreign_reads(bot, since)


async def test_the_bare_follow_up_re_asks_the_question_that_got_which_wallet(
    bot: E2EBot,
) -> None:
    """The production order: the address typed, a question the route could
    not place, then "the wallet I just gave you" -- that question, re-asked."""
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    await dm.say(f"qual o saldo de {ADDRESS}?")
    await dm.say("obrigado")
    await dm.say("que dia é hoje?")
    await dm.say("e o bitcoin, como está?")

    since = len(bot.web.calls)
    turn = await dm.say("the wallet I just gave you")

    assert turn.edge() == "CHAIN" and not turn.searched
    assert _reached(bot, since, ADDRESS)


async def test_in_a_channel_only_the_address_the_asker_typed_there_is_used(bot: E2EBot) -> None:
    leo, ana = bot.person("Leo"), bot.person("Ana")
    # Somebody else's address, in the same channel: asked about, and chatted.
    await bot.channel("general", ana).say(
        f"verifique as pools de liquidez nesta wallet {ANAS_ADDRESS}"
    )
    await bot.chatter(
        "general", ana, f"minha carteira nova é {ANAS_ADDRESS}", at=NOW - timedelta(minutes=5)
    )
    general = bot.channel("general", leo)
    await general.say(POOLS)

    since = len(bot.web.calls)
    why = await general.say(WHY_NOT_IN_MY_CURRENCY)
    again = await general.say(JUST_GAVE)

    assert why.edge() == "CHAIN" and again.edge() == "CHAIN"
    assert not why.searched and not again.searched
    assert _reached(bot, since, ADDRESS)
    _no_foreign_reads(bot, since)


async def test_an_address_only_somebody_else_typed_or_the_corpus_holds_is_never_used(
    bot: E2EBot,
) -> None:
    leo, ana = bot.person("Leo"), bot.person("Ana")
    await bot.channel("general", ana).say(
        f"verifique as pools de liquidez nesta wallet {ANAS_ADDRESS}"
    )
    await bot.chatter(
        "general", ana, f"minha carteira nova é {ANAS_ADDRESS}", at=NOW - timedelta(minutes=5)
    )
    # Leo's own address, typed in his DM: another location, not this one.
    await bot.dm(leo).say(f"qual o saldo de {ADDRESS}?")
    general = bot.channel("general", leo)

    since = len(bot.web.calls)
    mine = await general.say("quais as minhas pools de liquidez?")
    again = await general.say(JUST_GAVE)

    for turn in (mine, again):
        assert turn.edge() == "NONE" and not turn.searched
        assert "0x" in turn.text, "the reply asks for the address"
    _no_foreign_reads(bot, since)
    assert not _reached(bot, since, ADDRESS), "an address from the DM leaked into the channel"
    assert COLLEAGUE_CRYPTO not in mine.text + again.text
