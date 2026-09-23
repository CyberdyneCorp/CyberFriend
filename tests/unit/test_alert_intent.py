"""Recognising an alert request, in both languages, and nothing else.

A false positive answers a question with a Confirm button, and a false
negative sends an alert request to the corpus, which answers it from a
colleague's messages about their own positions. Both directions are tested.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from chatmemory.app.alert_intent import AlertIntent, alert_intent
from chatmemory.ports.alerts import AlertKind

RANGE, HEALTH = AlertKind.LP_RANGE, AlertKind.AAVE_HEALTH
ADDRESS = "0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("tell me when my LP goes out of range", RANGE),
        ("notify me when my uniswap position is out of range", RANGE),
        ("ping me once my v4 position leaves its range", RANGE),
        ("let me know when my pool goes outside the range", RANGE),
        ("alert me if my liquidity position falls out of range", RANGE),
        ("tel me wen my lp goes out of rnage", RANGE),
        ("avise quando minha posição sair da faixa", RANGE),
        ("avise quando minha posicao sair da faixa", RANGE),
        ("me avisa quando meu LP ficar fora do range", RANGE),
        ("me notifica se a minha pool sair do intervalo", RANGE),
        ("quero ser avisado quando minha posição na uniswap sair da faixa", RANGE),
        ("alert me if my health factor drops below 1.25 on base", HEALTH),
        ("let me know if my HF falls under 1.4", HEALTH),
        ("set up an alert for my aave health factor below 1.5", HEALTH),
        ("notfy me if hf < 1.2", HEALTH),
        ("me avisa se o health factor do aave cair abaixo de 1.3", HEALTH),
        ("me avisa se o health factor cair abaixo de 1,3", HEALTH),
        ("me aviza se o hf cair abaixo de 1,15", HEALTH),
        ("quero um alerta se o fator de saúde cair para 1,2", HEALTH),
        ("crie um alerta pro meu health factor abaixo de 1,4", HEALTH),
        # Put politely, to the assistant: still a request.
        ("can you alert me when my LP goes out of range?", RANGE),
        ("hey, could you tell me when my hf drops below 1.2", HEALTH),
        ("você pode me avisar quando minha posição sair da faixa?", RANGE),
        ("pode me avisar se o health factor cair abaixo de 1,3?", HEALTH),
        ("what does hf mean? tell me when my hf drops below 1.2", HEALTH),
    ],
)
def test_requests_are_recognised(text: str, kind: AlertKind) -> None:
    found = alert_intent(text)
    assert found is not None and found.kind is kind


@pytest.mark.parametrize(
    "text",
    [
        # About the archive: conversation verbs veto, as for the wallet routes.
        "what did people say about alerts",
        "what did people say about alerting when the LP goes out of range",
        "o que disseram sobre avisar quando a posição sair da faixa?",
        # An alert verb about something else.
        "tell me when the meeting starts",
        "me avisa quando o deploy terminar",
        "alert me when the build is out of range",
        # A reading, not a watch: the positions route answers these.
        "what is my health factor",
        "tell me if my health factor is ok",
        "tell me if my LP is in range",
        "is my LP out of range?",
        "qual é o meu health factor?",
        "how do alerts work?",
        # Questions about alerting, not requests for an alert.
        "does uniswap notify me when my position goes out of range?",
        "is there a bot that can alert me when my LP goes out of range?",
        "which app can warn me when my aave health factor gets low?",
        "what is the best tool to create an alert for health factor?",
        "can revert finance notify me if my health factor drops below 1.2?",
        "revert finance can notify me if my health factor drops below 1.2",
        "como criar um alerta quando a posição sair da faixa no revert?",
        "qual app consegue me avisar quando o health factor cair?",
        # The health factor is in another sentence from the request.
        "what does hf mean? tell me when you know",
        "",
    ],
)
def test_everything_else_is_not(text: str) -> None:
    assert alert_intent(text) is None


@pytest.mark.parametrize(
    ("text", "threshold"),
    [
        ("alert me if my health factor drops below 1.25", Decimal("1.25")),
        ("me avisa se o health factor cair abaixo de 1,3", Decimal("1.3")),
        ("notify me when my HF gets to 1.1", Decimal("1.1")),
        ("me avisa se o hf chegar a 1,5", Decimal("1.5")),
        # Carried as written, so the refusal can say which bounds it is outside.
        ("alert me if my health factor drops below 0.9", Decimal("0.9")),
        ("alert me if my health factor drops below 10", Decimal("10")),
        ("set an alert on my health factor", None),
    ],
)
def test_the_limit_is_read_as_written(text: str, threshold: Decimal | None) -> None:
    found = alert_intent(text)
    assert found is not None and found.threshold == threshold


def test_an_address_and_a_version_are_not_the_limit() -> None:
    found = alert_intent(f"alert me if the v3 health factor of {ADDRESS} drops below 1.3")
    assert found == AlertIntent(HEALTH, Decimal("1.3"), address=ADDRESS.lower())


@pytest.mark.parametrize(
    ("text", "chain"),
    [
        ("alert me if my health factor drops below 1.3 on base", "base"),
        ("me avisa se o health factor do aave na arbitrum cair abaixo de 1,3", "arbitrum"),
        ("tell me when my LP on ethereum goes out of range", "ethereum"),
        ("tell me when my LP on mainnet goes out of range", "ethereum"),
        ("tell me when my LP goes out of range", None),
    ],
)
def test_a_named_chain_narrows_the_read(text: str, chain: str | None) -> None:
    found = alert_intent(text)
    assert found is not None and found.chain == chain


def test_one_position_can_be_named_by_its_number() -> None:
    found = alert_intent("tell me when position #4558452 goes out of range")
    assert found is not None and found.token_id == 4558452


def test_an_address_the_asker_typed_before_is_carried() -> None:
    """Their own earlier question, as typed: an address rooted in their words."""
    previous = (f"what positions does {ADDRESS} have?",)
    found = alert_intent("tell me when that position goes out of range", previous)
    assert found is not None
    assert found.address == ADDRESS.lower()


def test_without_an_address_the_caller_decides() -> None:
    found = alert_intent("tell me when my LP goes out of range", ("what's the weather?",))
    assert found is not None and found.address is None
