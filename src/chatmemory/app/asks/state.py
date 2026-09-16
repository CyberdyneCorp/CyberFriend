"""Whether an ask is still outstanding, decided by events that happened.

Nothing in this module calls a model. "Does this look resolved?" is more
expensive than looking, less consistent between runs, and unauditable -- and
when it is wrong it either hides an obligation or invents one. The two signals
used here either occurred or did not: the addressee replied in-thread after the
ask, or they marked it with an acknowledging reaction.

Ageing is a third state rather than closure. An ask nobody answered in three
weeks is stale; closing it silently would hide exactly what the person asking
"what do I need to do" wanted to see.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import structlog

from chatmemory.app.asks.model import (
    Ask,
    AskPolicy,
    AskStatus,
    ClosedBy,
    StateRefresh,
)
from chatmemory.app.asks.ports import AskStore
from chatmemory.domain.identity import PersonRef
from chatmemory.domain.messages import Message

log = structlog.get_logger()

#: Reactions that mean "received, I have it".
#:
#: Eyes (👀) is excluded on purpose: it means someone is looking, which is the
#: opposite of finished, and treating it as acknowledgement closes asks that
#: are actively being worked on. Both unicode and platform shortcodes appear,
#: because which one arrives depends on how the event was captured.
ACKNOWLEDGING_REACTIONS = frozenset(
    {
        "✅",
        "☑️",
        "✔️",
        "👍",
        "👌",
        "🫡",
        "🆗",
        "white_check_mark",
        "heavy_check_mark",
        "ballot_box_with_check",
        "thumbsup",
        "+1",
        "ok_hand",
        "saluting_face",
        "ok",
    }
)


@dataclass(frozen=True, slots=True)
class Reaction:
    """An observed reaction on the message an ask came from."""

    person: PersonRef
    emoji: str
    at: datetime


def _bare(emoji: str) -> str:
    """`emoji` stripped of the selectors that only choose how it is drawn.

    U+FE0F and U+FE0E ask for the emoji or the text presentation of the same
    character, so "☑️" and "☑" are the same reaction wearing different bytes.
    Which one arrives depends on the client that sent it.
    """
    return emoji.strip().replace("️", "").replace("︎", "")


_BY_BARE: dict[str, str] = {_bare(e): e for e in ACKNOWLEDGING_REACTIONS}


def canonical_reaction(emoji: str) -> str | None:
    """The set member `emoji` denotes, or None when it acknowledges nothing.

    Callers record the *canonical* form rather than what arrived, because the
    statement that closes an ask compares the stored emoji against exactly this
    set. A reaction kept in the other spelling would sit in the table for ever
    and close nothing -- an ask that looks answered to the person who answered
    it and open to everybody else.
    """
    return _BY_BARE.get(_bare(emoji))


def is_acknowledging(emoji: str) -> bool:
    return canonical_reaction(emoji) is not None


@dataclass(frozen=True, slots=True)
class Transition:
    status: AskStatus
    at: datetime | None = None
    by: ClosedBy | None = None


def next_state(
    ask: Ask,
    replies: Sequence[Message] = (),
    reactions: Sequence[Reaction] = (),
    now: datetime | None = None,
    policy: AskPolicy | None = None,
) -> Transition:
    """The state an ask is in, given what has been observed since it was made.

    Pure, so the rules can be read and tested without a database, and so the
    SQL that applies them at scale has something to be checked against.
    """
    settings = policy or AskPolicy()
    addressee = ask.addressee.person
    if addressee is None:
        # Nobody is on the hook, so nothing anybody does can answer it.
        return _age(ask, now, settings)

    answered = _first_reply(ask, addressee, replies)
    if answered is not None:
        return Transition(AskStatus.ANSWERED, answered, ClosedBy.REPLY)

    acknowledged = _first_acknowledgement(ask, addressee, reactions)
    if acknowledged is not None:
        return Transition(AskStatus.ANSWERED, acknowledged, ClosedBy.REACTION)

    return _age(ask, now, settings)


def _first_reply(ask: Ask, addressee: PersonRef, replies: Sequence[Message]) -> datetime | None:
    """The addressee's first message in the same thread after the ask.

    In-thread, or a direct reply to the asking message. A later message from
    them elsewhere in the channel is not an answer: people talk about other
    things in the same room, and treating that as closure loses real asks.
    """
    candidates = [
        m.created_at
        for m in replies
        if m.is_visible
        and m.author == addressee
        and m.created_at > ask.asked_at
        and m.channel == ask.channel
        and (
            m.reply_to_id == ask.source_message_id
            or (ask.thread_id is not None and m.thread_id == ask.thread_id)
        )
    ]
    return min(candidates) if candidates else None


def _first_acknowledgement(
    ask: Ask, addressee: PersonRef, reactions: Sequence[Reaction]
) -> datetime | None:
    candidates = [
        r.at for r in reactions if r.person == addressee and is_acknowledging(r.emoji)
    ]
    return min(candidates) if candidates else None


def _age(ask: Ask, now: datetime | None, policy: AskPolicy) -> Transition:
    if ask.status is AskStatus.ANSWERED:
        return Transition(AskStatus.ANSWERED, ask.closed_at, ask.closed_by)
    at = now or ask.asked_at
    if at - ask.asked_at >= policy.stale_after:
        return Transition(AskStatus.STALE)
    return Transition(ask.status)


class AskStateService:
    """Applies the observable transitions across the whole corpus."""

    def __init__(self, store: AskStore, policy: AskPolicy | None = None) -> None:
        self._store = store
        self._policy = policy or AskPolicy()

    async def record_reaction(
        self, source_message_id: int, person: PersonRef, emoji: str, at: datetime
    ) -> bool:
        """Keep a reaction that could close an ask; ignore the rest.

        Storing only acknowledging reactions keeps this from becoming a general
        record of who reacted to what, which is not what anyone consented to.

        Whose reaction it was is not checked here, and must not be: the
        addressee of an ask is a property of a row, and re-deciding it in the
        adapter would be a second copy of the rule. `CLOSE_ANSWERED_BY_REACTION`
        binds `person_id = addressee_person_id` as a predicate, so a reaction
        from anybody else is stored and closes nothing.

        Idempotent by construction. The same reaction arriving twice is one row
        (the store conflicts it away) and one transition (the close only fires
        on an ask that is still outstanding).
        """
        canonical = canonical_reaction(emoji)
        if canonical is None:
            return False
        await self._store.record_reaction(source_message_id, person, canonical, at)
        return True

    async def refresh(self, now: datetime) -> StateRefresh:
        refreshed = await self._store.refresh_state(
            now, self._policy.stale_after, ACKNOWLEDGING_REACTIONS
        )
        if refreshed.total:
            log.info(
                "asks.state_refreshed",
                answered_by_reply=refreshed.answered_by_reply,
                answered_by_reaction=refreshed.answered_by_reaction,
                marked_stale=refreshed.marked_stale,
            )
        return refreshed
