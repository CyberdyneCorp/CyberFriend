"""Naming the people the corpus only knows by account id."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import discord

from chatmemory.adapters.discord.names import name_people
from chatmemory.domain.identity import PersonRef

GUILD = 1
MEMBER, LEFT, DELETED = PersonRef("discord", 10), PersonRef("discord", 20), PersonRef("discord", 30)


class Store:
    def __init__(self, unnamed: Sequence[PersonRef]) -> None:
        self.unnamed = list(unnamed)
        self.named: dict[PersonRef, str] = {}

    async def unnamed_people(self, limit: int) -> Sequence[PersonRef]:
        return self.unnamed[:limit]

    async def resolve_person(self, person: PersonRef, display_name: str) -> int:
        self.named[person] = display_name
        return 1


class Client:
    """A member cache holding one person, and a user lookup for one who left."""

    def __init__(self) -> None:
        self.fetched: list[int] = []

    def get_guild(self, guild_id: int, /) -> Any:
        members = {10: SimpleNamespace(global_name="Edvane", display_name="ed", name="ed")}
        return SimpleNamespace(get_member=members.get) if guild_id == GUILD else None

    async def fetch_user(self, user_id: int, /) -> Any:
        self.fetched.append(user_id)
        if user_id == 20:
            return SimpleNamespace(global_name=None, display_name="", name="bea_old")
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown User")


async def test_members_and_former_members_are_named_and_the_rest_left_alone() -> None:
    store, client = Store([MEMBER, LEFT, DELETED]), Client()

    assert await name_people(client, store, GUILD) == 2  # type: ignore[arg-type]

    assert store.named == {MEMBER: "Edvane", LEFT: "bea_old"}
    assert client.fetched == [20, 30], "the member cache answers before any lookup"


async def test_a_store_failure_is_logged_and_never_raised() -> None:
    class Broken(Store):
        async def unnamed_people(self, limit: int) -> Sequence[PersonRef]:
            raise RuntimeError("database down")

    assert await name_people(Client(), Broken([]), GUILD) == 0  # type: ignore[arg-type]
