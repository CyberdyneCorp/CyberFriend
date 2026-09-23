"""The alert prompt's expiry, and what `/alert list` says about each alert.

The press itself -- the asker's Confirm, somebody else's refused, Cancel --
runs end to end in `tests/e2e/test_alert_requests.py`. Here: a prompt nobody
answered retires its buttons, and the listing makes silence legible (checked,
failing, stopped) in both languages.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import discord

from chatmemory.adapters.discord.alerts import (
    AlertConfirmView,
    alert_listing,
    locale_language,
)
from chatmemory.app.alert_requests import AlertProposal
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.alerts import (
    POSITION_CLOSED,
    AlertKind,
    AlertLanguage,
    AlertState,
    LpProtocol,
    LpTarget,
    PositionAlert,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LEO = PersonRef("discord", 1)
EN, PT = AlertLanguage.ENGLISH, AlertLanguage.PORTUGUESE


class Message:
    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)


def proposal(language: AlertLanguage = EN) -> AlertProposal:
    return AlertProposal(LEO, language, True, (), "Here's what I'll watch:")


async def test_an_unanswered_prompt_expires_and_loses_its_buttons() -> None:
    async def confirm(_: AlertProposal) -> str:
        raise AssertionError("an expired prompt creates nothing")

    view = AlertConfirmView(proposal(PT), confirm, timeout=1)
    message = Message()
    view.sent_as(message)  # type: ignore[arg-type]

    await view.on_timeout()

    [edit] = message.edits
    assert edit["content"] == "Este pedido expirou e nada foi criado. Peça de novo se ainda quiser."
    assert all(
        item.disabled for item in view.children if isinstance(item, discord.ui.Button)
    )
    assert [b.label for b in view.children if isinstance(b, discord.ui.Button)] == [
        "Confirmar",
        "Cancelar",
    ]


def alert(**changes: Any) -> PositionAlert:
    base = PositionAlert(
        id=12,
        person=LEO,
        kind=AlertKind.AAVE_HEALTH,
        chain="base",
        address="0xdd8a0000000000000000000000000000000063d6",
        language=EN,
        state=AlertState.OK,
        state_since=NOW - timedelta(hours=2),
        created_at=NOW - timedelta(hours=2),
        next_check_at=NOW,
        threshold=Decimal("1.3"),
        last_value=Decimal("1.43"),
        last_checked_at=NOW - timedelta(minutes=3),
    )
    return replace(base, **changes)


def test_the_listing_says_what_each_alert_watches_and_whether_it_is_working() -> None:
    lp = LpTarget(LpProtocol.UNISWAP_V3, 4558452, "0xpool", "WETH", "USDC", 18, 6, 500)
    listing = alert_listing(
        [
            alert(),
            alert(id=13, consecutive_failures=12),
            alert(
                id=14,
                kind=AlertKind.LP_RANGE,
                lp=lp,
                threshold=None,
                state=AlertState.CLOSED,
                disabled_at=NOW,
                disabled_reason=POSITION_CLOSED,
            ),
        ],
        EN,
    )

    lines = listing.splitlines()
    assert lines[0] == "**Your alerts (3):**"
    assert lines[1].startswith(
        "**12** - Aave health factor on Base below 1.30 - above the limit, HF 1.43 since <t:"
    )
    assert lines[2].startswith("  last checked <t:")
    assert lines[4] == "  failing: 12 reads in a row could not be made"
    assert lines[5].startswith("**14** - Uniswap v3 WETH/USDC 0.05% #4558452 on Base - closed")
    assert lines[6] == "  stopped - position closed"
    assert lines[-1] == "-# `/alert delete` stops one."


def test_the_listing_in_portuguese() -> None:
    listing = alert_listing(
        [alert(disabled_at=NOW, disabled_reason=POSITION_CLOSED, last_checked_at=None)], PT
    )

    assert "**12** - health factor no Aave na Base abaixo de 1,30 - acima do limite" in listing
    assert "  parado - posição fechada" in listing
    assert alert_listing([], PT).startswith("Você não tem alertas.")


def test_the_listing_language_follows_the_discord_client() -> None:
    assert locale_language(discord.Locale.brazil_portuguese) is PT
    assert locale_language(discord.Locale.american_english) is EN
    assert locale_language("pt-BR") is PT
