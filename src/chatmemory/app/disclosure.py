"""Bounding an answer by the audience that will receive it.

Retrieval is scoped by audience at source, but this module re-checks every
citation immediately before delivery. That redundancy is deliberate: a defect
in the answer service leaks to a whole channel rather than to one person, and
the check is cheap. Nothing reaches Discord without passing through here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import structlog

from chatmemory.domain.audience import Audience
from chatmemory.domain.identity import ChannelRef, Viewer
from chatmemory.ports.answers import Answer


@dataclass(frozen=True, slots=True)
class ScopedAnswer:
    """An answer cleared for delivery, plus what the asker alone may be told."""

    answer: Answer
    withheld_from_audience: frozenset[ChannelRef]

    @property
    def should_notify_asker(self) -> bool:
        """Whether the asker gets a private note that a fuller answer exists.

        Only when the asker could themselves have seen the withheld evidence.
        Telling them about content they also cannot read would disclose it.
        """
        return bool(self.withheld_from_audience)


def enforce_audience(answer: Answer, audience: Audience, asker: Viewer) -> ScopedAnswer:
    """Drop any citation the audience may not read, and report what was dropped.

    The returned answer is safe to deliver to `audience`. The withheld set is
    intersected with the asker's own visibility, so the notice never reveals
    the existence of content the asker also cannot read.
    """
    permitted = tuple(c for c in answer.citations if audience.permits(c.channel))
    dropped = frozenset(
        c.channel for c in answer.citations if not audience.permits(c.channel)
    )

    # Evidence the service already knew it had withheld, plus anything this
    # guard caught. Restricted to what the asker may read: a notice about
    # content the asker cannot see is itself a disclosure.
    withheld = (dropped | answer.withheld_channels) & asker.visible_channels

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
