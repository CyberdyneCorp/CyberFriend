"""Bounding an answer by the audience that will receive it.

Retrieval is scoped by audience at source, but this module re-checks every
citation immediately before delivery. That redundancy is deliberate: a defect
in the answer service leaks to a whole channel rather than to one person, and
the check is cheap. Nothing reaches Discord without passing through here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

import structlog

from chatmemory.domain.audience import Audience
from chatmemory.domain.identity import ChannelRef, Viewer
from chatmemory.ports.answers import Answer


class WithheldEvidenceProbe(Protocol):
    """Reports what scoping an answer to its audience cost this asker.

    Retrieval is pre-scoped to asker INTERSECT audience, so by the time an
    answer exists the evidence the audience may not read was never gathered
    and nothing downstream can tell that anything was missed. Something has
    to look, and this is the only thing that does.

    It returns channel identities and nothing else. What it inspects is by
    definition what the audience may not receive, and the answer it informs
    is delivered to that audience: returning evidence here would put a single
    misplaced assignment between private content and a public reply.
    """

    async def withheld_channels(
        self, asker: Viewer, audience: Audience, text: str
    ) -> frozenset[ChannelRef]:
        """Channels the asker may read, the audience may not, that hold
        content relevant to `text`. Empty when there is nothing to tell."""
        ...


@dataclass(frozen=True, slots=True)
class ScopedAnswer:
    """An answer cleared for delivery, plus what the asker alone may be told."""

    answer: Answer
    withheld_from_audience: frozenset[ChannelRef]
    #: A fixed notice the surface shows after the answer and its sources, such
    #: as the one-time tracing notice. Kept apart from the answer so it is
    #: never remembered as part of it, truncated with it, or cited under.
    notice: str = ""

    @property
    def should_notify_asker(self) -> bool:
        """Whether the asker gets a private note that a fuller answer exists.

        Only when the asker could themselves have seen the withheld evidence.
        Telling them about content they also cannot read would disclose it.
        """
        return bool(self.withheld_from_audience)


def enforce_audience(
    answer: Answer,
    audience: Audience,
    asker: Viewer,
    *,
    withheld_by_scoping: frozenset[ChannelRef] = frozenset(),
) -> ScopedAnswer:
    """Drop any citation the audience may not read, and report what was dropped.

    The returned answer is safe to deliver to `audience`. The withheld set is
    intersected with the asker's own visibility, so the notice never reveals
    the existence of content the asker also cannot read.

    `withheld_by_scoping` is what never reached the answer at all, because
    retrieval was scoped to the audience before it ran. Without it the
    withheld set could only ever contain what this guard itself had to drop
    -- which, when the service scopes correctly, is nothing.
    """
    # Channel permissions apply to corpus evidence only. A web result belongs
    # to no channel, so testing it for channel membership refuses it always.
    permitted = tuple(
        c for c in answer.citations if not c.is_corpus or audience.permits(c.channel)
    )
    dropped = frozenset(
        c.channel
        for c in answer.citations
        if c.is_corpus and not audience.permits(c.channel)
    )

    # Three sources: what retrieval never gathered, what the service knew it
    # had withheld, and what this guard caught. Restricted to what the asker
    # may read, because a notice about content the asker cannot see is itself
    # a disclosure -- and to what the audience may not, because a channel the
    # room can read was not withheld from it and saying so would be a lie.
    withheld = (
        (dropped | answer.withheld_channels | withheld_by_scoping) & asker.visible_channels
    ) - audience.readable_channels

    text = answer.text
    if dropped:
        # The answer service is supposed to scope retrieval by audience, so
        # reaching here means it did not. Dropping the offending citations is
        # not enough: `text` was synthesized from evidence that included them,
        # and a paraphrase of a private conversation discloses it just as
        # effectively as a quote -- more so, since it carries no link a reader
        # could fail to open. Prose cannot be filtered after the fact, so the
        # whole answer is suppressed rather than trimmed.
        log.error(
            "disclosure.upstream_scoping_failure",
            dropped_channels=sorted(str(c) for c in dropped),
            audience_mode=audience.mode,
        )
        text = SUPPRESSED_ANSWER

    cleared = replace(
        answer,
        text=text,
        citations=() if dropped else permitted,
        # The public answer must give no sign anything was withheld.
        withheld_channels=frozenset(),
        # Deliberately keyed on `dropped` alone. `withheld_by_scoping` must
        # leave the delivered answer byte-identical to the one a restricted
        # asker would have got: any field that varies with it is a channel
        # through which the room learns that private content exists.
        partial=answer.partial or bool(dropped),
    )
    return ScopedAnswer(answer=cleared, withheld_from_audience=withheld)


log = structlog.get_logger()

SUPPRESSED_ANSWER = (
    "I can't answer that here. Ask me in a direct message and I'll give you "
    "what you're allowed to see."
)
"""Shown when audience scoping had to discard evidence.

Deliberately identical whether the cause was restricted evidence or an
upstream bug, and deliberately says nothing about what was found.
"""

WITHHELD_NOTICE = (
    "I left some things out of that answer because not everyone in "
    "{destination} can see them. Ask me here and I'll give you the full version."
)


def withheld_notice(destination_name: str) -> str:
    return WITHHELD_NOTICE.format(destination=destination_name)
