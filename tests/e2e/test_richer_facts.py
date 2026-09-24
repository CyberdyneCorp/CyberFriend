"""A whole introduction, as it was sent in production, through the assembled bot.

Before this change the DM below saved the full name and nothing else, and told
Leo his phone number was "too long". Every fact it states is asserted here by
SQL on the real table, and the reply is the string Discord received.
"""

from __future__ import annotations

from typing import cast

from chatmemory.adapters.discord.source import RawMessage, is_ingestable, to_message
from tests.e2e.harness.conversation import E2EBot

INTRODUCTION = (
    "Oi Me chamo Leonardo Araujo dos Santos pode me Chamar de Leo, tenho 45 anos "
    "nasci em 21/06/1981 meu telefone e +5521980703795 morro no Rio de Janeiro "
    "Brasil, Vargem Grande minha walet e 0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0 "
    "meu email leonardoaraujo.santos@gmail.com"
)
WALLET = "0xb26b933a075fbb3d4e8b0925cad4f2bc345475e0"
EMAIL = "leonardoaraujo.santos@gmail.com"
PHONE = "+5521980703795"
ADDRESS = "Rio de Janeiro Brasil, Vargem Grande"
STORED = [
    ("full_name", "Leonardo Araujo dos Santos"),
    ("preferred_name", "Leo"),
    ("birth_date", "1981-06-21"),
    ("phone", PHONE),
    ("home_address", ADDRESS),
    ("eth_wallet", WALLET),
    ("email", EMAIL),
]
PRIVATE = ("980703795", "Vargem", "1981", "junho", WALLET, EMAIL)


async def test_the_production_introduction_in_a_dm_stores_every_fact(bot: E2EBot) -> None:
    leo = bot.person("Leo")

    turn = await bot.dm(leo).say(INTRODUCTION)

    assert await bot.fact_rows(leo) == STORED
    assert not turn.searched, "the introduction was answered from the corpus"
    assert turn.text.splitlines() == [
        "Aqui está o que salvei:",
        "✓ Nome completo: **Leonardo Araujo dos Santos**",
        "✓ Nome preferido: **Leo**",
        "✓ Data de nascimento: **21 de junho de 1981**",
        f"✓ Telefone: **{PHONE}**",
        f"✓ Endereço: **{ADDRESS}**",
        f"✓ Carteira Ethereum: **{WALLET}**",
        f"✓ E-mail: `{EMAIL}`",
        "✗ Idade: não guardo, ela vem da sua data de nascimento",
    ]
    turn.assert_language("pt")
    # Nothing of it was kept as a conversation turn either.
    assert await bot.dm(leo).memory_turns() == []


async def test_what_do_you_know_about_me_lists_everything_in_a_dm(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    dm = bot.dm(leo)
    await dm.say(INTRODUCTION)
    await dm.say("minha carteira é 0xd8dA6BF26964aF9D7eEd9e03E53415d37aA96045")

    turn = await dm.say("o que você sabe sobre mim?")

    assert turn.text.startswith("Aqui está o que você me pediu para guardar:")
    for shown in (
        "Leonardo Araujo dos Santos", "**Leo**", "21 de junho de 1981", PHONE, ADDRESS,
        WALLET, "0xd8da6bf26964af9d7eed9e03e53415d37aa96045", EMAIL,
    ):
        assert shown in turn.text
    turn.assert_language("pt")


async def test_in_a_channel_it_is_stored_but_nothing_private_is_shown(bot: E2EBot) -> None:
    leo = bot.person("Leo")

    turn = await bot.channel("general", leo).say(INTRODUCTION)

    assert await bot.fact_rows(leo) == STORED
    for private in PRIVATE:
        assert private not in turn.text
    for hidden in ("Telefone", "Endereço", "Data de nascimento", "Carteira Ethereum", "E-mail"):
        assert f"✓ {hidden}: salvo (mostrado só em mensagem direta)" in turn.text
    assert "Leonardo Araujo dos Santos" in turn.text

    shown = await bot.channel("general", leo).say("o que você sabe sobre mim?")
    for private in PRIVATE:
        assert private not in shown.text


async def test_the_channel_message_is_withheld_from_the_corpus(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    general = bot.discord.channel("general")

    # A real discord.py message, as the gateway hands one to the source.
    stated = cast(RawMessage, bot.discord.channel_message(leo, general, INTRODUCTION))
    ordinary = cast(
        RawMessage, bot.discord.channel_message(leo, general, "the deploy is done, ship it")
    )

    # The one conversion live capture, edits and backfill all go through.
    assert to_message(stated) is None
    assert not is_ingestable(stated)
    assert to_message(ordinary) is not None
