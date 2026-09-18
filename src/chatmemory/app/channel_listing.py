"""Which archived channels one person may be told about.

The listing itself is an intersection, and the whole feature is which two sets
it takes. Archived channels come from live scope; readable channels come from
the same `AclResolver` that scopes retrieval. Using that resolver rather than a
second permission check is the point: a listing computed any other way could
disagree with what the person can actually search, and the disagreement would
be invisible until somebody noticed a channel they were told about returning
nothing -- or worse, one they were not told about returning something.

Nothing is reported about what was filtered out. Not the names, not the count,
not that there were any. A count is a disclosure: "4 archived channels you
cannot read" tells somebody private channels exist and how many, which is most
of what the ACL design exists to withhold. The reply is the same whether the
server has four private archived channels or none.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from chatmemory.app.scope import ScopeProvider
from chatmemory.domain.identity import ChannelRef, PersonRef
from chatmemory.ports.acl import AclResolver

log = structlog.get_logger()

PLATFORM = "discord"


@dataclass(frozen=True, slots=True)
class ChannelListing:
    """The archived channels one person may be shown, and nothing else.

    Deliberately carries no total, no "and N more" and no flag saying whether
    anything was withheld. A field that exists gets rendered eventually.
    """

    channels: tuple[ChannelRef, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.channels


class ChannelListingService:
    """Lists the archived channels a person can read."""

    def __init__(self, scope: ScopeProvider, acl: AclResolver) -> None:
        # A provider, not a set: scope changes without a redeploy, and a
        # listing built from a captured copy would name a channel somebody
        # un-indexed this morning.
        self._scope = scope
        self._acl = acl

    async def for_person(self, person: PersonRef) -> ChannelListing:
        """The archived channels this person can read, right now.

        Both halves are read per request rather than captured: scope changes
        without a redeploy, and access changes without anything happening here
        at all. A listing built from a snapshot would name a channel somebody
        lost access to this morning.
        """
        indexed = self._scope.current()
        if not indexed:
            return ChannelListing()
        # Fails closed by contract: a person who cannot be resolved to a
        # member gets an empty visible set, so they are shown nothing.
        viewer = await self._acl.resolve_viewer(person)
        readable = {c.platform_channel_id for c in viewer.visible_channels}
        shown = sorted(indexed & readable)
        log.info(
            "channels.listed",
            person=str(person),
            shown=len(shown),
            # The number withheld is deliberately not logged either: an
            # operator reading logs is not who this protects against, but a
            # field that exists is a field that gets surfaced later.
        )
        return ChannelListing(tuple(ChannelRef(PLATFORM, c) for c in shown))
