"""Every feature a deployment runs is in "what can you do?", and only those.

The description once named voice and `/alert` conditionally and nothing else:
decisions, catch-up, wallet activity, the portfolio and most personal facts
were live and unmentioned, so people asked "o que você pode fazer?" and were
never told. The table below is the guard: each row is a switch and the phrase
that switch must put in the reply, in both languages. Built through the same
composition the process uses, from settings to registered tools, so a feature
wired without a description line fails here.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from chatmemory.adapters.discord.bot import MAX_ANSWER_CHARS
from chatmemory.adapters.discord.formatting import DISCORD_MESSAGE_LIMIT, split_message
from chatmemory.app.alert_intent import alert_intent
from chatmemory.app.catchup import catch_up_request
from chatmemory.app.language import Language
from chatmemory.app.routing import (
    decision_question,
    fact_intent,
    obligation_question,
    self_description_question,
)
from chatmemory.app.self_description import _TEXT, Capabilities
from chatmemory.composition import build_federation, deployment_capabilities
from chatmemory.config import Settings

EN, PT = Language.ENGLISH, Language.PORTUGUESE

BASE: dict[str, object] = {
    "discord_token": "t",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://u:p@h/d",
    "llm_api_key": "k",
    "indexed_channel_ids": "100",
}

EVERYTHING: dict[str, object] = {
    "wallet_tools_enabled": True,
    "infura_key": "infura",
    "market_tools_enabled": True,
    "web_tools_enabled": True,
    "serpapi_key": "serp",
    "alerts_enabled": True,
    "scheduled_tasks_enabled": True,
    "notifications_enabled": True,
    "ask_extraction_enabled": True,
    "voice_questions_enabled": True,
    "media_api_key": "media",
}


@dataclass(frozen=True)
class Row:
    """A switch, what turns it off, and what it must add in each language."""

    feature: str
    off: dict[str, object]
    english: str
    portuguese: str


ROWS = (
    Row(
        "wallet balances",
        {"wallet_tools_enabled": False},
        "Balances on Ethereum, Base and Arbitrum",
        "Saldos na Ethereum, Base e Arbitrum",
    ),
    Row(
        "uniswap positions",
        {"wallet_tools_enabled": False},
        "Uniswap v3/v4",
        "Uniswap v3/v4",
    ),
    Row(
        "aave",
        {"wallet_tools_enabled": False},
        "Aave supplies, borrows and health factor",
        "Depósitos, empréstimos e health factor no Aave",
    ),
    Row(
        "portfolio",
        {"wallet_tools_enabled": False},
        "how much do I have in total?",
        "quanto eu tenho no total?",
    ),
    Row(
        "wallet activity",
        {"wallet_tools_enabled": False},
        "what did my wallet do this week?",
        "o que minha carteira fez essa semana?",
    ),
    Row(
        "no infura key, no wallet tools",
        {"infura_key": None, "alerts_enabled": False},
        "Balances on Ethereum",
        "Saldos na Ethereum",
    ),
    Row(
        "btc and eth prices",
        {"market_tools_enabled": False},
        "Bitcoin and Ether prices",
        "Bitcoin e do Ether",
    ),
    Row(
        "s&p 500",
        {"market_tools_enabled": False},
        "the S&P 500",
        "o S&P 500",
    ),
    Row(
        "fx",
        {"market_tools_enabled": False},
        "currency conversion",
        "conversão de moedas",
    ),
    Row(
        "web",
        {"web_tools_enabled": False},
        "the web (Wikipedia, Google)",
        "a web (Wikipedia, Google)",
    ),
    Row(
        "alerts",
        {"alerts_enabled": False},
        "**Alerts**",
        "**Alertas**",
    ),
    Row(
        "lp range alert",
        {"alerts_enabled": False},
        "tell me when my LP goes out of range",
        "me avisa quando meu LP ficar fora do range",
    ),
    Row(
        "lp edge alert",
        {"alerts_enabled": False},
        "within 10% of the range edge",
        "a 10% da borda do range",
    ),
    Row(
        "health factor alert",
        {"alerts_enabled": False},
        "alert me if my health factor drops below 1.3",
        "me avisa se o health factor cair abaixo de 1,3",
    ),
    Row(
        "price alert",
        {"alerts_enabled": False},
        "tell me when BTC goes above 100k",
        "avisa quando o BTC passar de 100k",
    ),
    Row(
        "alert commands",
        {"alerts_enabled": False},
        "`/alert list`",
        "`/alert list`",
    ),
    Row(
        "scheduled questions",
        {"scheduled_tasks_enabled": False},
        "`/schedule create`",
        "`/schedule create`",
    ),
    Row(
        "voice",
        {"voice_questions_enabled": False},
        "send me a voice message",
        "me mande um áudio",
    ),
    Row(
        "notifications",
        {"notifications_enabled": False},
        "`/notifications`",
        "`/notifications`",
    ),
    Row(
        "obligations",
        {"ask_extraction_enabled": False},
        "what do I need to do?",
        "o que eu preciso fazer?",
    ),
    Row(
        "asked today",
        {"ask_extraction_enabled": False},
        "what did people ask me today?",
        "o que me pediram hoje?",
    ),
)

FACTS = (
    # English phrase, Portuguese phrase: every kind a person can save.
    ("what to call you", "como quer ser chamado"),
    ("full name", "nome completo"),
    ("email", "e-mail"),
    ("phone", "telefone"),
    ("home address", "telefone, endereço"),
    ("birth date", "data de nascimento"),
    ("preferred language", "idioma preferido"),
    ("ETH and BTC wallets (several of each)", "carteiras ETH e BTC (várias de cada)"),
    ("Several in one message", "Várias de uma vez"),
    ("what do you know about me?", "o que você sabe sobre mim?"),
    ("forget my phone", "esqueça meu telefone"),
)

ALWAYS = (
    # Features with no switch in this process: always described.
    ("I cite the messages I used", "cito as mensagens que usei"),
    ("what did I miss in #infra?", "o que eu perdi no #general?"),
    ("what did Ana say about pricing yesterday?", "o que o João disse sobre o preço ontem?"),
    ("what did we decide about the deploy?", "o que decidimos sobre o deploy?"),
    ("I only cite what everyone there can read", "eu só cito o que todos ali podem ler"),
)


def settings(**overrides: object) -> Settings:
    return Settings.model_validate({**BASE, **EVERYTHING, **overrides})


async def capabilities(*, personal_facts: bool = True, **overrides: object) -> Capabilities:
    """What the process would describe: settings -> registered tools -> reply."""
    configured = settings(**overrides)
    federation = await build_federation(configured)
    try:
        tools = sorted(federation.federation.permits) if federation else []
        return deployment_capabilities(configured, tools, personal_facts=personal_facts)
    finally:
        if federation is not None:
            await federation.federation.aclose()


def phrase(row: Row | tuple[str, str], language: Language) -> str:
    if isinstance(row, Row):
        return row.english if language is EN else row.portuguese
    return row[0] if language is EN else row[1]


LANGUAGES = pytest.mark.parametrize("language", [EN, PT], ids=["en", "pt"])


@LANGUAGES
async def test_everything_on_describes_every_feature(language: Language) -> None:
    described = (await capabilities()).describe(language, direct_message=True)
    for row in (*ROWS, *FACTS, *ALWAYS):
        assert phrase(row, language) in described, f"{language}: missing {row}"


@LANGUAGES
@pytest.mark.parametrize("row", ROWS, ids=[r.feature for r in ROWS])
async def test_a_feature_switched_off_is_not_described(row: Row, language: Language) -> None:
    described = (await capabilities(**row.off)).describe(language, direct_message=True)
    assert phrase(row, language) not in described


@LANGUAGES
async def test_personal_facts_are_offered_only_where_they_are_kept(language: Language) -> None:
    described = (await capabilities(personal_facts=False)).describe(language)
    for row in FACTS:
        assert phrase(row, language) not in described


@LANGUAGES
async def test_nothing_switched_on_still_describes_the_conversations(language: Language) -> None:
    off = {key: False for key, value in EVERYTHING.items() if value is True}
    described = (await capabilities(personal_facts=False, **off)).describe(language)
    for row in ALWAYS:
        assert phrase(row, language) in described
    for row in ROWS:
        assert phrase(row, language) not in described


def _bodies() -> Iterator[tuple[Language, bool]]:
    for language in (EN, PT):
        for direct in (True, False):
            yield language, direct


@pytest.mark.parametrize(("language", "direct"), list(_bodies()))
async def test_it_fits_discord_and_splits_between_sections(
    language: Language, direct: bool
) -> None:
    """Split at paragraph boundaries into messages under 2000 characters,
    each section whole in one of them, and never clipped as an answer."""
    described = (await capabilities()).describe(language, direct_message=direct)
    assert len(described) <= MAX_ANSWER_CHARS
    sections = described.split("\n\n")
    messages = split_message(described)
    assert all(len(m) <= DISCORD_MESSAGE_LIMIT for m in messages)
    assert [s for m in messages for s in m.split("\n\n")] == sections


# --- every example is one the bot routes ---------------------------------------


def _quoted(key: str, language: Language) -> list[str]:
    lines: Any = _TEXT[language][key]
    return [q for line in lines for q in re.findall(r"`([^`]+)`", line)]


@LANGUAGES
def test_every_alert_example_makes_an_alert(language: Language) -> None:
    examples = _quoted("alerts", language)
    assert len(examples) == 4
    for example in examples:
        assert alert_intent(example) is not None, example


@LANGUAGES
def test_every_fact_example_is_a_fact_request(language: Language) -> None:
    examples = _quoted("facts", language)
    assert len(examples) == 3
    for example in examples:
        assert fact_intent(example) is not None, example


@LANGUAGES
def test_the_conversation_examples_reach_their_routes(language: Language) -> None:
    examples = _quoted("conversations", language)
    catch_up, decision = examples[1], examples[3]
    assert catch_up_request(catch_up) is not None
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    assert decision_question(decision, now, ZoneInfo("UTC")) is not None


def test_the_english_obligation_examples_reach_the_obligation_route() -> None:
    for example in _quoted("obligations", EN):
        assert obligation_question(example) is not None, example


@pytest.mark.parametrize("text", ["o que você pode fazer?", "what can you do?"])
def test_the_question_itself_is_recognised(text: str) -> None:
    assert self_description_question(text)
