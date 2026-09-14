"""Permission resolution ports.

Two distinct questions live here, and they must not share an implementation:

  AclResolver      - what may *this person* read?
  AudienceResolver - what may *everyone receiving this answer* read?

They resemble each other closely enough that reusing one for the other is the
likely defect, so they are separate protocols with separate tests.
"""

from __future__ import annotations

from typing import Protocol

from chatmemory.domain.audience import Audience
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer


class AclResolver(Protocol):
    """Resolves one person's own read access."""

    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        """Return the person's readable channels.

        Must fail closed: a person who cannot be resolved to a member of the
        server gets an empty visible set, never a permissive default.
        """
        ...


class AudienceResolver(Protocol):
    """Resolves the collective read access of everyone receiving an answer."""

    async def resolve_for_channel(self, destination: ChannelRef) -> Audience:
        """Return the audience of a public reply in `destination`.

        `readable_channels` is the intersection across every member who can
        read `destination`: a channel is included only if all of them can
        read it too.
        """
        ...

    async def resolve_private(self, person: PersonRef) -> Audience:
        """Return the audience of a reply only `person` will see."""
        ...
