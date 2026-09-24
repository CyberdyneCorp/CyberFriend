"""Answering "what did people ask me" and "what do I need to do".

These are questions about *state* -- who asked, of whom, is it still open --
not about topical resemblance, so they are answered by filtering records rather
than by embedding the question and hoping the right windows surface. That is
the whole reason extraction exists.

Every reported ask carries a citation back to the message it came from. An ask
is the system's inference about a person, so a reader has to be able to check
it in one click; an uncheckable claim about what somebody owes is worse than no
claim at all.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

import structlog

from chatmemory.app.asks.model import (
    AskKind,
    AskPolicy,
    AskStatus,
    ObligationRequest,
    ReportedAsk,
)
from chatmemory.app.asks.ports import AskStore
from chatmemory.domain.identity import ChannelRef, Viewer
from chatmemory.ports.answers import Answer, Citation

log = structlog.get_logger()

MessageUrl = Callable[[ChannelRef, int], str]

NOTHING_OUTSTANDING = "Nothing outstanding."

_PHRASING = {
    AskKind.REQUEST: "asked you to",
    AskKind.QUESTION: "asked you",
    AskKind.COMMITMENT: "you said you would",
}


def discord_message_url(guild_id: int) -> MessageUrl:
    """Deep link to a message, so a citation resolves in one click."""

    def build(channel: ChannelRef, message_id: int) -> str:
        return (
            f"https://discord.com/channels/{guild_id}/"
            f"{channel.platform_channel_id}/{message_id}"
        )

    return build


def _no_url(channel: ChannelRef, message_id: int) -> str:
    return f"{channel}/{message_id}"


class ObligationService:
    """Reads obligations from state. Runs no similarity search."""

    def __init__(
        self,
        store: AskStore,
        policy: AskPolicy | None = None,
        message_url: MessageUrl = _no_url,
    ) -> None:
        self._store = store
        self._policy = policy or AskPolicy()
        self._url = message_url

    async def asked_of_me(
        self,
        viewer: Viewer,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> Answer:
        """What other people asked of the viewer in a period.

        Commitments are excluded: the viewer's own promises are things they
        said, not things that were asked of them, and mixing them makes the
        answer to "what did people ask me today" unverifiable.
        """
        request = self._request(
            since=since,
            until=until,
            kinds=frozenset({AskKind.REQUEST, AskKind.QUESTION}),
            include_commitments=False,
            limit=limit,
        )
        found = await self._store.obligations(viewer, request)
        return self._render(found, viewer, limit)

    async def what_i_need_to_do(self, viewer: Viewer, limit: int = 50) -> Answer:
        """Open asks addressed to the viewer, plus their own open commitments."""
        request = self._request(
            kinds=frozenset(AskKind), include_commitments=True, limit=limit
        )
        found = await self._store.obligations(viewer, request)
        return self._render(found, viewer, limit)

    async def outstanding_count(self, viewer: Viewer) -> int:
        """How many things are outstanding, counted under the same filter.

        Asks in channels the viewer may not read are not counted: a number that
        includes them reports the existence of a private conversation just as
        surely as quoting it would.
        """
        return await self._store.count_outstanding(viewer, self._request())

    def _request(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        kinds: frozenset[AskKind] | None = None,
        include_commitments: bool = True,
        limit: int = 50,
    ) -> ObligationRequest:
        return ObligationRequest(
            since=since,
            until=until,
            kinds=kinds if kinds is not None else frozenset(AskKind),
            include_commitments=include_commitments,
            statuses=(AskStatus.OPEN, AskStatus.STALE),
            # Taken from policy, never from a caller: a sub-threshold
            # extraction must not become an obligation because something
            # downstream asked for more results.
            min_confidence=self._policy.min_confidence,
            limit=limit,
        )

    def _render(self, found: Sequence[ReportedAsk], viewer: Viewer, limit: int) -> Answer:
        if not found:
            # A successful answer, not an abstention: "nothing" is the true
            # answer to the question, and abstaining would read as a failure.
            # It is a statement about every channel searched, so the turn is
            # recalled only while the viewer can still read all of them. Left
            # undeclared, memory refused to store the turn at all.
            return Answer(
                text=NOTHING_OUTSTANDING, consulted_channels=frozenset(viewer.visible_channels)
            )

        citations = tuple(self._cite(item) for item in found)
        lines = [
            f"{index}. {self._line(item)} [{index}]"
            for index, item in enumerate(found, start=1)
        ]
        heading = f"{len(found)} outstanding:"
        log.info(
            "asks.reported",
            viewer=str(viewer.person),
            reported=len(found),
            channels=len({item.ask.channel for item in found}),
        )
        return Answer(
            text="\n".join([heading, *lines]),
            citations=citations,
            partial=len(found) >= limit,
            # Declared, or memory drops the turn and "and the second one?"
            # has nothing to refer to.
            consulted_channels=frozenset(item.ask.channel for item in found),
        )

    def _line(self, item: ReportedAsk) -> str:
        ask = item.ask
        when = ask.asked_at.date().isoformat()
        stale = " (stale)" if ask.status is AskStatus.STALE else ""
        if ask.kind is AskKind.COMMITMENT:
            return f"{when}: {_PHRASING[ask.kind]} {ask.text}{stale}"
        return f"{when}: {item.requester_display} {_PHRASING[ask.kind]} {ask.text}{stale}"

    def _cite(self, item: ReportedAsk) -> Citation:
        ask = item.ask
        return Citation(
            channel=ask.channel,
            message_id=ask.source_message_id,
            author_display=item.requester_display,
            # The original words, not the extraction: the point of a citation
            # is to let a reader check what the model claimed against what was
            # actually written.
            excerpt=item.source_excerpt[: self._policy.excerpt_chars],
            url=self._url(ask.channel, ask.source_message_id),
        )
