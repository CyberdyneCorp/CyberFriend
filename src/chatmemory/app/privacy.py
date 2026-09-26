"""`/privacy`: what the assistant holds about the person asking.

One read across every store (`PrivacyStore.inventory`), plus the facts about
retention that make the reply honest: how long questions and answers are
traced, how long other people's remembered answers last, and whether database
backups exist. Those come from settings, never from text written here, so the
reply cannot promise a period the deployment does not keep.

Archive coverage comes from `ChannelListingService`, the same intersection of
live scope and readable channels that `/channels` shows. Only those channels
are read for message and media counts, so a channel the person can no longer
read is neither named nor counted. Without a listing, no channel is.

`erase` is [Delete everything...]: it reads the same inventory for the counts
the reply gives, then hands over to `app.erasure`, which deletes everything
of the person's in every channel, readable or not.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from chatmemory.app.channel_listing import ChannelListingService
from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.erasure import ErasureService
from chatmemory.app.voice import month_of
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.privacy import (
    ErasureCounts,
    ErasureMode,
    ErasureRequest,
    Inventory,
    PrivacyStore,
)

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class RetentionFacts:
    """What `/privacy` states about keeping data, as the deployment is configured."""

    tracing: bool
    """Whether questions and answers are exported to the trace store now."""
    trace_retention_days: int
    memory_retention_days: int
    backup_retention_days: int | None
    """None: no database backups are kept."""


@dataclass(frozen=True, slots=True)
class PrivacyReport:
    inventory: Inventory
    retention: RetentionFacts


class PrivacyService:
    """Builds one person's privacy report, for them alone."""

    def __init__(
        self,
        store: PrivacyStore,
        retention: RetentionFacts,
        channels: ChannelListingService | None = None,
        clock: Clock = utc_now,
        erasure: ErasureService | None = None,
    ) -> None:
        self._store = store
        self._retention = retention
        self._channels = channels
        self._clock = clock
        self._erasure = erasure

    @property
    def can_erase(self) -> bool:
        """Whether [Delete everything...] is offered: False without an erasure path."""
        return self._erasure is not None

    async def report(self, person: PersonRef) -> PrivacyReport:
        readable = await self._readable_channel_ids(person)
        month = month_of(self._clock().date())
        inventory = await self._store.inventory(person, readable, month)
        log.info("privacy.reported", person=str(person), known=inventory.known)
        return PrivacyReport(inventory, self._retention)

    async def erase(self, person: PersonRef, mode: ErasureMode) -> ErasureRequest:
        """Delete everything held about `person` (`app.erasure`).

        The counts the reply gives are read first, from the same inventory the
        dashboard shows, so messages in channels the person cannot read now
        are deleted but not counted.
        """
        if self._erasure is None:
            raise RuntimeError("erasure is not wired in this process")
        report = await self.report(person)
        return await self._erasure.erase(person, mode, ErasureCounts.of(report.inventory))

    async def _readable_channel_ids(self, person: PersonRef) -> tuple[int, ...]:
        if self._channels is None:
            return ()
        listing = await self._channels.for_person(person)
        return tuple(c.platform_channel_id for c in listing.channels)
