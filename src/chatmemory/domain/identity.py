"""Identities, and the scope an answer is computed under.

`Viewer` and `Audience` are deliberately distinct types. A viewer is one
person and answers the question "what may this person read". An audience is
the set of people who will *receive* an answer, and answers the narrower
question "what may all of them read". Conflating them is how a public reply
becomes a disclosure: see openspec answer-disclosure.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PersonRef:
    """A person, identified by their account on a platform."""

    platform: str
    platform_user_id: int

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.platform}:{self.platform_user_id}"


@dataclass(frozen=True, slots=True)
class ChannelRef:
    platform: str
    platform_channel_id: int

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.platform}:{self.platform_channel_id}"


@dataclass(frozen=True, slots=True)
class Viewer:
    """The person on whose behalf retrieval happens.

    Required with no default everywhere it is used. A retrieval context that
    can be built without one is the exfiltration bug.
    """

    person: PersonRef
    visible_channels: frozenset[ChannelRef]

    def may_read(self, channel: ChannelRef) -> bool:
        return channel in self.visible_channels
