"""Letting people fix what extraction got wrong about them.

Two rules, both load-bearing:

*Corrections outrank extraction, permanently.* Without a persisted override, an
ask somebody dismissed reappears the next time its source is reprocessed, which
reads as the system ignoring them -- and that is the point at which people stop
reading the list at all.

*Only the addressee may correct their own asks.* Otherwise marking someone
else's obligations done becomes a way to hide them.
"""

from __future__ import annotations

import structlog

from chatmemory.app.asks.model import (
    Ask,
    Correction,
    CorrectionOutcome,
    CorrectionResolution,
)
from chatmemory.app.asks.ports import AskStore
from chatmemory.domain.identity import PersonRef, Viewer

log = structlog.get_logger()


def may_correct(ask: Ask, person: PersonRef) -> bool:
    """Whether `person` may correct this ask.

    Group-directed and unattributed asks are nobody's to correct: there is no
    addressee, so there is no one whose statement about it outranks the record.
    Letting any member of a group close a group ask would let one person
    silently clear an obligation the rest still hold.
    """
    return ask.addressee.person == person


class CorrectionService:
    def __init__(self, store: AskStore) -> None:
        self._store = store

    async def mark_done(self, viewer: Viewer, ask_key: str) -> CorrectionOutcome:
        return await self.correct(viewer, ask_key, CorrectionResolution.DONE)

    async def mark_not_applicable(self, viewer: Viewer, ask_key: str) -> CorrectionOutcome:
        return await self.correct(viewer, ask_key, CorrectionResolution.NOT_APPLICABLE)

    async def correct(
        self, viewer: Viewer, ask_key: str, resolution: CorrectionResolution
    ) -> CorrectionOutcome:
        """Record the viewer's correction to one of their own asks.

        The corrector is taken from the viewer rather than from an argument, so
        correcting on somebody else's behalf is not expressible; the store
        additionally binds it as a predicate, so the check is not a step that
        can be skipped by a caller.
        """
        outcome = await self._store.apply_correction(
            viewer, Correction(ask_key=ask_key, by=viewer.person, resolution=resolution)
        )
        log.info(
            "asks.correction",
            ask_key=ask_key,
            by=str(viewer.person),
            resolution=resolution,
            outcome=outcome,
        )
        return outcome
