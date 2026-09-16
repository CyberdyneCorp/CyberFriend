"""The asker's own Discord profile, read from the gateway cache.

This reads the same member the permission resolver reads -- through the same
guild provider and the same `get_member` lookup in `acl` -- so the profile a
prompt sees and the access a retrieval runs under come from one source of
truth. It does not compute anything about permissions; roles appear here only
by name, as something the model may use to resolve "my team".

The lookup is for one person, and the only person anyone passes is the asker.
There is no method that takes a guild and returns many profiles, because the
use that would serve -- describing a colleague from their roles -- is excluded
by design rather than left to a prompt to decline.

Every failure answers None. A cold member cache, a person from another
platform, a member object missing an attribute: each means answering without
the profile, and none of them is a reason to fail a question.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog

from chatmemory.adapters.discord.acl import PLATFORM, GuildProvider, _Guild, static_guild
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.answers import AskerProfile

log = structlog.get_logger()

# Discord caps nicknames at 32 and role names at 100 characters. The caps here
# are a backstop in case a cache or a fake hands us something longer; they bound
# how much attacker-typed text one profile can put in a prompt.
MAX_NAME_CHARS = 100
# A member with hundreds of roles would otherwise turn the profile into the
# largest block in the prompt. Highest roles are kept, as the most descriptive.
MAX_ROLES = 20

_EVERYONE = "@everyone"


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped[:MAX_NAME_CHARS] if stripped else None


def _is_default_role(role: object) -> bool:
    # @everyone is a role every member holds; naming it says nothing about the
    # asker and spends prompt on noise.
    is_default = getattr(role, "is_default", None)
    if callable(is_default):
        return bool(is_default())
    return getattr(role, "name", None) == _EVERYONE


def _role_names(roles: object) -> tuple[str, ...]:
    if not isinstance(roles, Iterable) or isinstance(roles, (str, bytes)):
        return ()
    names = [
        name
        for role in roles
        if not _is_default_role(role)
        and (name := _text(getattr(role, "name", None))) is not None
    ]
    # discord.py orders `Member.roles` lowest first; the prompt wants highest.
    return tuple(reversed(names))[:MAX_ROLES]


class DiscordProfileResolver:
    """Resolves one person's own profile from the cached guild member."""

    def __init__(self, guild: GuildProvider | _Guild) -> None:
        self._guild = guild if callable(guild) else static_guild(guild)

    async def resolve_profile(self, person: PersonRef) -> AskerProfile | None:
        try:
            return self._resolve(person)
        except Exception:  # the gateway cache is not a reason to fail a question
            log.warning("profile.resolve_failed", user_id=person.platform_user_id, exc_info=True)
            return None

    def _resolve(self, person: PersonRef) -> AskerProfile | None:
        guild = self._guild()
        if person.platform != PLATFORM or guild is None:
            return None
        member = guild.get_member(person.platform_user_id)
        if member is None or member.id != person.platform_user_id:
            # The second check is cheap insurance for the invariant this module
            # exists under: the profile returned is the person asked about.
            log.info("profile.member_unresolved", user_id=person.platform_user_id)
            return None

        nickname = _text(getattr(member, "nick", None))
        display = (
            _text(getattr(member, "global_name", None))
            or _text(getattr(member, "name", None))
            or nickname
        )
        if display is None:
            return None
        return AskerProfile(
            person=person,
            display_name=display,
            nickname=nickname,
            role_names=_role_names(getattr(member, "roles", ())),
        )
