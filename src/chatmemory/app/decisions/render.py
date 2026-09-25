"""The decision reply, rendered from rows with no model.

Dated, because decisions supersede each other and nothing here models that:
the reader sees the order and judges. Newest first, so the one most likely to
stand is read first. Each line cites the message that settled it -- the
original words, not the summary -- so a reader can check the system's claim
in one click.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import tzinfo

from chatmemory.app.asks.obligations import MessageUrl
from chatmemory.app.decisions.model import DecisionPolicy, ReportedDecision
from chatmemory.app.language import Language
from chatmemory.app.routing import DecisionQuestion
from chatmemory.app.said_by import span_words
from chatmemory.ports.answers import Answer, Citation

_TEXT: dict[Language, dict[str, str]] = {
    Language.PORTUGUESE: {
        "about": "Decisões sobre {topic}{when}:",
        "period": "Decisões{when}:",
        "recent": "Decisões dos últimos {days} dias:",
        "date": "%d/%m/%Y",
    },
    Language.ENGLISH: {
        "about": "Decisions about {topic}{when}:",
        "period": "Decisions{when}:",
        "recent": "Decisions in the last {days} days:",
        "date": "%d %b %Y",
    },
}


def _words(language: Language) -> dict[str, str]:
    return _TEXT.get(language, _TEXT[Language.ENGLISH])


def _heading(asked: DecisionQuestion, tz: tzinfo, policy: DecisionPolicy) -> str:
    words = _words(asked.language)
    when = span_words(asked.span, tz, asked.language)
    if asked.topic:
        return words["about"].format(topic=asked.topic, when=when)
    if asked.span is not None:
        return words["period"].format(when=when)
    return words["recent"].format(days=policy.default_days)


def render_decisions(
    found: Sequence[ReportedDecision],
    asked: DecisionQuestion,
    *,
    tz: tzinfo,
    policy: DecisionPolicy,
    message_url: MessageUrl,
) -> Answer:
    """One numbered line per decision, each citing its source message."""
    date = _words(asked.language)["date"]
    lines = [_heading(asked, tz, policy)]
    lines += [
        f"{index}. {item.decided_at.astimezone(tz):{date}} — "
        f"{item.author_display}: {item.summary} [{index}]"
        for index, item in enumerate(found, start=1)
    ]
    citations = tuple(
        Citation(
            channel=item.channel,
            message_id=item.source_message_id,
            author_display=item.author_display,
            excerpt=item.source_excerpt[: policy.excerpt_chars],
            url=message_url(item.channel, item.source_message_id),
        )
        for item in found
    )
    return Answer(
        text="\n".join(lines),
        citations=citations,
        # Declared, as for obligations, or memory drops the turn and "and the
        # second one?" has nothing to refer to.
        consulted_channels=frozenset(item.channel for item in found),
    )
