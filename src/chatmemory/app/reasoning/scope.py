"""Who a run retrieves as.

An answer is bounded by the audience that will receive it, not by the person
who asked: a public reply may only rest on channels everyone able to read the
destination can also read. Retrieval therefore runs as the asker *intersected
with* the audience -- never as the asker alone, which would gather evidence
the delivery guard would then have to strip.

There is deliberately no branch here on the result being empty. A viewer who
can read nothing must take exactly the same path as a viewer whose corpus
happens to hold nothing: an early exit would make the access-blocked case
observably faster, which reintroduces the disclosure that one shared wording
closes.
"""

from __future__ import annotations

from chatmemory.domain.identity import Viewer
from chatmemory.ports.answers import Question


def retrieval_viewer(question: Question) -> Viewer:
    """The viewer every retrieval in a run is scoped to, for the whole run."""
    return Viewer(
        person=question.asker.person,
        visible_channels=question.asker.visible_channels & question.audience.readable_channels,
    )
