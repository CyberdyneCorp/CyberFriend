"""The set of people who will receive an answer.

The audience — not the asker — bounds what evidence an answer may contain.
An answer delivered publicly in a channel may only draw on channels that
*everyone* able to read the destination can also read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from chatmemory.domain.identity import ChannelRef, PersonRef


class DeliveryMode(StrEnum):
    """Where an answer will be seen."""

    PUBLIC_CHANNEL = "public_channel"
    DIRECT_MESSAGE = "direct_message"
    EPHEMERAL = "ephemeral"


@dataclass(frozen=True, slots=True)
class Audience:
    """Who will see an answer, and what they may collectively read.

    `readable_channels` is the *intersection* over the audience's members:
    a channel appears only if every member of the audience can read it.
    For a single-person audience this coincides with that person's own
    visibility, which is why direct messages need no special case.
    """

    mode: DeliveryMode
    members: frozenset[PersonRef]
    readable_channels: frozenset[ChannelRef]
    destination: ChannelRef | None = None

    def permits(self, source: ChannelRef) -> bool:
        """Whether evidence from `source` may appear in an answer to this audience."""
        return source in self.readable_channels

    def filter_sources(self, sources: frozenset[ChannelRef]) -> frozenset[ChannelRef]:
        return frozenset(s for s in sources if self.permits(s))

    @property
    def is_private(self) -> bool:
        return self.mode in (DeliveryMode.DIRECT_MESSAGE, DeliveryMode.EPHEMERAL)


def private_audience(person: PersonRef, visible: frozenset[ChannelRef]) -> Audience:
    """An audience of one. Used for direct messages and ephemeral replies."""
    return Audience(
        mode=DeliveryMode.DIRECT_MESSAGE,
        members=frozenset({person}),
        readable_channels=visible,
    )


EMPTY_AUDIENCE = Audience(
    mode=DeliveryMode.EPHEMERAL,
    members=frozenset(),
    readable_channels=frozenset(),
)
"""Fail-closed default. An audience that could not be resolved reads nothing."""
