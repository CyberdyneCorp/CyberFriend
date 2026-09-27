"""The bot's half of a link: "Linked to a***@..., not you? [Unlink]".

A notice is marked sent once the DM went out, or once it never can (the DM is
closed, the account is gone); a Discord blip leaves it for the next pass. The
[Unlink] button is persistent, so it works after a restart.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import discord
import pytest

from chatmemory.adapters.discord.accounts import UNLINK_ID, LinkAnnouncer, UnlinkView
from chatmemory.app.accounts import AccountService, LinkAnnouncements
from chatmemory.app.language import Language
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.accounts import LinkNotice

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
LEO = PersonRef("discord", 7)
NOTICE = LinkNotice(LEO, "sub-leo", "l***@example.com")


class Store:
    def __init__(self) -> None:
        self.pending = [NOTICE]
        self.marked: list[LinkNotice] = []

    async def unannounced_links(self, limit: int) -> Sequence[LinkNotice]:
        return [n for n in self.pending if n not in self.marked]

    async def mark_announced(self, notice: LinkNotice, now: datetime) -> None:
        self.marked.append(notice)


class _Response:
    status = 403
    reason = "no"


class User:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, Any]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append((content, kwargs.get("view")))


def _accounts() -> AccountService:
    return AccountService(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        email_key=b"k" * 32,
        link_base_url="https://admin.example.com/",
    )


async def _portuguese(person: PersonRef) -> Language:
    return Language.PORTUGUESE


def _announcer(store: Store, user: User) -> LinkAnnouncer:
    async def fetch(user_id: int) -> User:
        assert user_id == LEO.platform_user_id
        return user

    return LinkAnnouncer(
        LinkAnnouncements(store, clock=lambda: NOW),  # type: ignore[arg-type]
        _accounts(),
        fetch,
        _portuguese,
    )


async def test_the_notice_names_the_masked_email_and_offers_unlink() -> None:
    store, user = Store(), User()

    assert await _announcer(store, user).announce() == 1

    [(content, view)] = user.sent
    assert r"l\*\*\*@example.com" in content and "Desvincular" in content
    assert "https://admin.example.com/#/me" in content
    assert isinstance(view, UnlinkView) and view.is_persistent()
    assert [item.custom_id for item in view.children] == [UNLINK_ID]  # type: ignore[attr-defined]
    assert store.marked == [NOTICE]


async def test_a_closed_dm_is_not_retried() -> None:
    store = Store()
    user = User(discord.Forbidden(_Response(), "cannot send"))  # type: ignore[arg-type]

    assert await _announcer(store, user).announce() == 1
    assert store.marked == [NOTICE]


@pytest.mark.parametrize("status", [500, 429])
async def test_a_discord_blip_is_tried_again(status: int) -> None:
    response = _Response()
    response.status = status
    store = Store()
    user = User(discord.HTTPException(response, "busy"))  # type: ignore[arg-type]

    assert await _announcer(store, user).announce() == 0
    assert store.marked == []
