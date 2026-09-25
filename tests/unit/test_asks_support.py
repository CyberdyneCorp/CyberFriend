"""Builders and an in-memory ask store shared by the ask unit tests.

The fake store enforces the same rules the SQL does -- viewer scoping,
corrections outranking extraction, tombstoned sources dropping out -- so a test
that passes here is testing the behaviour rather than the absence of a
database. The integration tests prove the SQL agrees.

Contains no tests of its own; it is named `test_asks_support` so it sits with
the change's other files.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from chatmemory.app.asks.model import (
    UNATTRIBUTED,
    Addressee,
    AddresseeSignal,
    Ask,
    AskCandidate,
    AskKind,
    AskStatus,
    Correction,
    CorrectionOutcome,
    ExtractedAsk,
    Extraction,
    ObligationRequest,
    ReportedAsk,
    StateRefresh,
    ask_key,
    to_person,
)
from chatmemory.app.asks.state import Reaction, next_state
from chatmemory.app.decisions.model import Decision, ExtractedDecision
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message

PLATFORM = "discord"

ALICE = PersonRef(PLATFORM, 1)
BOB = PersonRef(PLATFORM, 2)
CARA = PersonRef(PLATFORM, 3)
BOT = PersonRef(PLATFORM, 99)

OPEN_CHANNEL = ChannelRef(PLATFORM, 100)
PRIVATE_CHANNEL = ChannelRef(PLATFORM, 200)
DM_CHANNEL = ChannelRef(PLATFORM, 300)

T0 = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


def viewer(person: PersonRef, *channels: ChannelRef) -> Viewer:
    return Viewer(person=person, visible_channels=frozenset(channels))


def message(
    message_id: int,
    author: PersonRef = ALICE,
    content: str = "hello",
    at: datetime | None = None,
    channel: ChannelRef = OPEN_CHANNEL,
    mentions: frozenset[PersonRef] = frozenset(),
    reply_to_id: int | None = None,
    thread_id: int | None = None,
    deleted_at: datetime | None = None,
) -> Message:
    return Message(
        platform_message_id=message_id,
        channel=channel,
        author=author,
        content=content,
        created_at=at or T0,
        deleted_at=deleted_at,
        reply_to_id=reply_to_id,
        thread_id=thread_id,
        mentions=mentions,
    )


def candidate(
    source: Message,
    signal: AddresseeSignal = AddresseeSignal.MENTION,
    reply_parent: Message | None = None,
) -> AskCandidate:
    return AskCandidate(message=source, signal=signal, reply_parent=reply_parent)


def extracted(
    kind: AskKind = AskKind.REQUEST,
    text: str = "review the migration",
    confidence: float = 0.9,
    addressee_hint: str | None = None,
    addressee_is_group: bool = False,
) -> ExtractedAsk:
    return ExtractedAsk(
        kind=kind,
        text=text,
        confidence=confidence,
        addressee_hint=addressee_hint,
        addressee_is_group=addressee_is_group,
    )


def ask(
    source_message_id: int = 10,
    requester: PersonRef = ALICE,
    addressee: Addressee | None = None,
    kind: AskKind = AskKind.REQUEST,
    text: str = "review the migration",
    confidence: float = 0.9,
    asked_at: datetime | None = None,
    channel: ChannelRef = OPEN_CHANNEL,
    status: AskStatus = AskStatus.OPEN,
    thread_id: int | None = None,
) -> Ask:
    target = addressee if addressee is not None else to_person(BOB)
    return Ask(
        key=ask_key(source_message_id, kind, target),
        source_message_id=source_message_id,
        channel=channel,
        requester=requester,
        addressee=target,
        kind=kind,
        text=text,
        confidence=confidence,
        asked_at=asked_at or T0,
        status=status,
        thread_id=thread_id,
    )


class StubExtractor:
    """Returns a fixed set of extractions, and counts how often it was asked."""

    def __init__(
        self,
        *results: ExtractedAsk,
        decisions: Sequence[ExtractedDecision] = (),
        fail: bool = False,
    ) -> None:
        self.results = list(results)
        self.decisions = list(decisions)
        self.calls: list[AskCandidate] = []
        self.fail = fail

    async def extract(self, candidate: AskCandidate) -> Extraction:
        self.calls.append(candidate)
        if self.fail:
            raise RuntimeError("endpoint unavailable")
        return Extraction(asks=tuple(self.results), decisions=tuple(self.decisions))


class FakeDecisionStore:
    """In-memory `DecisionStore`: upsert then prune, per source message."""

    def __init__(self, fail: bool = False) -> None:
        self.decisions: dict[str, Decision] = {}
        self.withdrawn: list[int] = []
        self.fail = fail

    async def record_decisions(
        self, source_message_id: int, decisions: Sequence[Decision]
    ) -> int:
        if self.fail:
            raise RuntimeError("database unavailable")
        keep = {d.key for d in decisions}
        for key in [
            k
            for k, d in self.decisions.items()
            if d.source_message_id == source_message_id and k not in keep
        ]:
            del self.decisions[key]
        self.decisions.update({d.key: d for d in decisions})
        return len(decisions)

    async def withdraw(self, source_message_ids: Sequence[int]) -> int:
        ids = set(source_message_ids)
        self.withdrawn.extend(source_message_ids)
        gone = [k for k, d in self.decisions.items() if d.source_message_id in ids]
        for key in gone:
            del self.decisions[key]
        return len(gone)


class FakeAskStore:
    """In-memory `AskStore` that keeps the rules the SQL keeps."""

    def __init__(self) -> None:
        self.asks: dict[str, Ask] = {}
        self.corrections: dict[str, Correction] = {}
        self.reactions: list[tuple[int, Reaction]] = []
        self.messages: dict[int, Message] = {}
        self.deleted: set[int] = set()
        self.writes = 0

    # --- writes ---------------------------------------------------------

    async def record_asks(self, source_message_id: int, asks: Sequence[Ask]) -> int:
        self.writes += 1
        keep = {a.key for a in asks}
        for key, existing in list(self.asks.items()):
            if (
                existing.source_message_id == source_message_id
                and key not in keep
                and key not in self.corrections
            ):
                del self.asks[key]
        for item in asks:
            previous = self.asks.get(item.key)
            # Matches the upsert: observed state is never reset by re-extraction.
            self.asks[item.key] = item if previous is None else _merge(previous, item)
        return len(asks)

    async def record_reaction(
        self,
        source_message_id: int,
        person: PersonRef,
        emoji: str,
        at: datetime,
        acknowledging: frozenset[str] = frozenset(),
    ) -> int:
        self.reactions.append((source_message_id, Reaction(person, emoji, at)))
        return 0

    async def remove_reaction(
        self,
        source_message_id: int,
        person: PersonRef,
        emoji: str,
        acknowledging: frozenset[str] = frozenset(),
    ) -> int:
        before = len(self.reactions)
        self.reactions = [
            (mid, r)
            for mid, r in self.reactions
            if not (mid == source_message_id and r.person == person and r.emoji == emoji)
        ]
        return before - len(self.reactions)

    async def refresh_state(
        self, now: datetime, stale_after: timedelta, acknowledging: frozenset[str]
    ) -> StateRefresh:
        by_reply = by_reaction = stale = 0
        for key, item in list(self.asks.items()):
            reactions = [r for mid, r in self.reactions if mid == item.source_message_id]
            transition = next_state(
                item, list(self.messages.values()), reactions, now
            )
            if transition.status is item.status:
                continue
            self.asks[key] = Ask(
                **{
                    **_fields(item),
                    "status": transition.status,
                    "closed_at": transition.at,
                    "closed_by": transition.by,
                }
            )
            if transition.status is AskStatus.STALE:
                stale += 1
            elif transition.by is not None and transition.by.value == "reply":
                by_reply += 1
            else:
                by_reaction += 1
        return StateRefresh(by_reply, by_reaction, stale)

    # --- reads ----------------------------------------------------------

    async def obligations(
        self, viewer_: Viewer, request: ObligationRequest
    ) -> Sequence[ReportedAsk]:
        found = [
            ReportedAsk(
                ask=item,
                requester_display=str(item.requester.platform_user_id),
                source_excerpt=self.messages.get(
                    item.source_message_id, message(item.source_message_id)
                ).content,
            )
            for item in self._matching(viewer_, request)
        ]
        found.sort(key=lambda r: r.ask.asked_at, reverse=True)
        return found[: request.limit]

    async def count_outstanding(self, viewer_: Viewer, request: ObligationRequest) -> int:
        return len(self._matching(viewer_, request))

    async def apply_correction(
        self, viewer_: Viewer, correction: Correction
    ) -> CorrectionOutcome:
        item = self.asks.get(correction.ask_key)
        if (
            item is None
            or not viewer_.may_read(item.channel)
            or item.source_message_id in self.deleted
        ):
            return CorrectionOutcome.UNKNOWN_ASK
        if item.addressee.person != viewer_.person or correction.by != viewer_.person:
            return CorrectionOutcome.NOT_ADDRESSEE
        self.corrections[correction.ask_key] = correction
        return CorrectionOutcome.APPLIED

    def _matching(self, viewer_: Viewer, request: ObligationRequest) -> list[Ask]:
        return [
            item
            for item in self.asks.values()
            if viewer_.may_read(item.channel)
            and item.source_message_id not in self.deleted
            and item.key not in self.corrections
            and item.status in request.statuses
            and item.kind in request.kinds
            and item.confidence >= request.min_confidence
            and (
                item.addressee.person == viewer_.person
                or (
                    request.include_commitments
                    and item.kind is AskKind.COMMITMENT
                    and item.requester == viewer_.person
                )
            )
            and (request.since is None or item.asked_at >= request.since)
            and (request.until is None or item.asked_at <= request.until)
        ]


def _fields(item: Ask) -> dict[str, object]:
    return {
        "key": item.key,
        "source_message_id": item.source_message_id,
        "channel": item.channel,
        "requester": item.requester,
        "addressee": item.addressee,
        "kind": item.kind,
        "text": item.text,
        "confidence": item.confidence,
        "asked_at": item.asked_at,
        "status": item.status,
        "thread_id": item.thread_id,
        "closed_at": item.closed_at,
        "closed_by": item.closed_by,
        "ask_id": item.ask_id,
    }


def _merge(previous: Ask, fresh: Ask) -> Ask:
    """Re-extraction updates content and leaves observed state alone."""
    return Ask(
        **{
            **_fields(fresh),
            "status": previous.status,
            "closed_at": previous.closed_at,
            "closed_by": previous.closed_by,
        }
    )


__all__ = [
    "ALICE",
    "BOB",
    "BOT",
    "CARA",
    "DM_CHANNEL",
    "OPEN_CHANNEL",
    "PLATFORM",
    "PRIVATE_CHANNEL",
    "T0",
    "UNATTRIBUTED",
    "FakeAskStore",
    "StubExtractor",
    "ask",
    "candidate",
    "extracted",
    "message",
    "viewer",
]
