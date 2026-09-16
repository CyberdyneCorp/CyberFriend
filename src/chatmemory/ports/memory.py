"""The conversation-memory port.

A person's conversation with the assistant, kept so a follow-up can be
interpreted. Three properties are fixed by the shape of this interface rather
than left to an implementation to remember:

*   **Reads take a `Viewer`, and the viewer is the key.** There is no
    `person` argument on `recall`: whose conversation is read is the viewer's
    person, and what survives is decided by the viewer's *current* readable
    channels. Reading somebody else's conversation, or reading one's own
    without a permission check, is therefore not expressible.

*   **A conversation is (person, location), and a location says whether it is
    a direct message.** A DM answer was scoped to one person; a channel answer
    is scoped to everyone present. Keying on the kind as well as the id means
    DM memory cannot reach a channel answer even if two ids ever collided.

*   **Every remembered item carries the channels it drew on, and is dropped
    whole if any of them is no longer readable.** A remembered answer quotes
    content the person could read *then*; replaying it after their access is
    revoked would bypass the revocation. A summary carries the union of what
    it replaced, and fails as a unit -- editing a summary to remove one
    channel's contribution would need to know which sentence came from where.

*   **Provenance travels with what is recalled.** A follow-up's answer is
    written by a model that was shown the remembered turns, and can restate
    them while its own evidence is somewhere else entirely. The turn it
    produces therefore inherits the channels of everything it was shown;
    without that, one follow-up would launder a revoked channel's content into
    a turn whose recorded channels are all still readable.

Remembered text is DATA. It interprets a follow-up; it is never cited and never
the source of a claim. Nothing here marks it safe to follow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer


@dataclass(frozen=True, slots=True)
class ConversationLocation:
    """Where a conversation is happening.

    `direct` is part of the identity, not a label on it: the same person's DM
    and channel conversations are different conversations, and must stay so.
    """

    platform: str
    platform_location_id: int
    direct: bool

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        kind = "dm" if self.direct else "channel"
        return f"{self.platform}:{kind}:{self.platform_location_id}"


@dataclass(frozen=True, slots=True)
class RememberedTurn:
    """One question and the answer given to it.

    `turn_id` is the store's ordering key; a summariser names the newest turn
    it covered by it. `source_channels` has already been checked by the time a
    turn is returned, and is not there to be re-checked -- a caller doing so
    against a different set would be making a second, weaker decision. It is
    there to be *inherited*: a turn answered with this one in the prompt drew
    on these channels too. No default, so a store cannot return a turn whose
    provenance it forgot to read.
    """

    turn_id: int
    question: str
    answer: str
    asked_at: datetime
    source_channels: frozenset[ChannelRef]


@dataclass(frozen=True, slots=True)
class RememberedSummary:
    """A model-written digest of older turns. Context, never evidence.

    `covered_channels` is the union the store computed when it wrote the
    summary, returned for the same reason a turn's `source_channels` is.
    """

    text: str
    through_turn_id: int
    created_at: datetime
    covered_channels: frozenset[ChannelRef]


@dataclass(frozen=True, slots=True)
class Recollection:
    """What a person may be reminded of right now, oldest first."""

    summaries: tuple[RememberedSummary, ...] = ()
    turns: tuple[RememberedTurn, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.summaries and not self.turns

    @property
    def source_channels(self) -> frozenset[ChannelRef]:
        """Every channel behind anything in here -- what showing it to a model costs."""
        return frozenset().union(
            *(s.covered_channels for s in self.summaries),
            *(t.source_channels for t in self.turns),
        )


@dataclass(frozen=True, slots=True)
class MemoryPurge:
    turns: int = 0
    summaries: int = 0

    @property
    def total(self) -> int:
        return self.turns + self.summaries


class MemoryStore(Protocol):
    async def record_turn(
        self,
        person: PersonRef,
        location: ConversationLocation,
        question: str,
        answer: str,
        source_channels: frozenset[ChannelRef],
    ) -> bool:
        """Store one turn with the channels it drew on.

        Returns False when nothing was stored -- the person has opted out, and
        the database dropped the row. `source_channels` has no default: an
        empty set asserts "drew on no channel", which is a claim, and a caller
        that has not worked out provenance must not be able to make it by
        omission.
        """
        ...

    async def recall(
        self, viewer: Viewer, location: ConversationLocation, turn_limit: int
    ) -> Recollection:
        """The viewer's own conversation here, filtered by what they read now.

        The filter is in the statement, not applied to what it returns, so the
        limit counts only turns that survive it.
        """
        ...

    async def record_summary(
        self,
        person: PersonRef,
        location: ConversationLocation,
        text: str,
        through_turn_id: int,
    ) -> bool:
        """Replace turns up to `through_turn_id`, and earlier summaries, with `text`.

        The covered channels are computed by the store from the rows being
        replaced, never supplied by the caller: a summariser that under-reported
        them would launder a revoked channel's content past the check. Returns
        False when nothing was written -- a newer summary already exists, there
        was nothing to replace, or the person has opted out.
        """
        ...

    async def forget(
        self, person: PersonRef, location: ConversationLocation | None
    ) -> MemoryPurge:
        """Delete this person's history here, or everywhere when `location` is None."""
        ...

    async def purge_before(self, cutoff: datetime) -> MemoryPurge:
        """Retention: delete every turn, and every summary with content, older than cutoff."""
        ...
