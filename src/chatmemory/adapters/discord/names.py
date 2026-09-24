"""Name the people the corpus only knows by account id.

Ingest writes the author's name with every message it captures, but it only
started doing so in 2f95d17. Everyone whose messages were backfilled before
that is still stored as "907306713009492040": a citation shows the number,
and a question by name ("o que o João disse ontem?") finds nobody and falls
to the ordinary route, which has no date filter. Their messages are never
captured again, so nothing else would ever fix the row.

Run once, when the gateway first connects. The guild's member cache answers
for anyone still in the server; Discord's user lookup answers for people who
left. A person neither can name keeps the id, and is tried again at the next
start.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import discord
import structlog

from chatmemory.adapters.discord.source import _display_name
from chatmemory.domain.identity import PersonRef

log = structlog.get_logger()

MAX_NAMED_PER_RUN = 500
"""A bound on one run's user lookups, which Discord rate-limits."""


class PeopleNames(Protocol):
    """The two store methods naming needs; `PostgresStore` has both."""

    async def unnamed_people(self, limit: int) -> Sequence[PersonRef]: ...

    async def resolve_person(self, person: PersonRef, display_name: str) -> int: ...


class UserDirectory(Protocol):
    """The slice of `discord.Client` naming reads."""

    def get_guild(self, guild_id: int, /) -> discord.Guild | None: ...

    async def fetch_user(self, user_id: int, /) -> discord.User: ...


async def _name_of(client: UserDirectory, guild_id: int, user_id: int) -> str:
    guild = client.get_guild(guild_id)
    member = guild.get_member(user_id) if guild is not None else None
    if member is not None:
        return _display_name(member)
    try:
        return _display_name(await client.fetch_user(user_id))
    except discord.HTTPException:  # NotFound for a deleted account included
        return ""


async def name_people(
    client: UserDirectory, store: PeopleNames, guild_id: int, limit: int = MAX_NAMED_PER_RUN
) -> int:
    """Give every person stored under their account id a name; how many got one.

    Never raises: a failure here leaves citations showing ids, which is how
    they were before, and is no reason to stop ingesting.
    """
    named = 0
    try:
        for person in await store.unnamed_people(limit):
            name = await _name_of(client, guild_id, person.platform_user_id)
            if name:
                await store.resolve_person(person, name)
                named += 1
    except Exception:  # noqa: BLE001 - see the docstring
        log.warning("ingest.naming_failed", named=named, exc_info=True)
    log.info("ingest.people_named", named=named)
    return named
