"""What a decision is, as extracted and as stored.

Like an ask, a decision is a claim the *system* made about a conversation, so
the row carries what a reader needs to check it: the message it is attributed
to, every message the model was shown alongside it, and the model's own
confidence, kept rather than thresholded away at write time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from chatmemory.domain.identity import ChannelRef, PersonRef


@dataclass(frozen=True, slots=True)
class ExtractedDecision:
    """One model output, before any of it is trusted.

    `summary` is a neutral one-line restatement in the conversation's
    language; `topic` is a few keywords, and is what the decision's identity
    is derived from.
    """

    summary: str
    topic: str
    confidence: float


@dataclass(frozen=True, slots=True)
class Decision:
    """An extracted decision as stored.

    `evidence_message_ids` is the source message plus every message the model
    was shown with it. The summary may carry words from any of them -- the
    proposal it settles, most often -- so deleting or opting out of one of
    those messages has to reach the decision too.
    """

    key: str
    source_message_id: int
    channel: ChannelRef
    author: PersonRef
    summary: str
    topic: str
    confidence: float
    decided_at: datetime
    evidence_message_ids: tuple[int, ...] = ()
    thread_id: int | None = None

    @property
    def embedding_text(self) -> str:
        """What is embedded: the topic leads, because it is what gets asked about."""
        return f"{self.topic}: {self.summary}"


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    """The thresholds this feature is tuned by.

    As for asks, `min_confidence` gates presentation, not storage: a weak
    extraction is still recorded, because seeing it is how the threshold is
    tuned, and it simply never reaches an answer.
    """

    min_confidence: float = 0.7

    def presentable(self, confidence: float) -> bool:
        return confidence >= self.min_confidence


def topic_slug(topic: str) -> str:
    """The topic's contribution to the decision key. Stable across runs.

    Accents are kept rather than folded: "decisão" and "decisao" are the same
    word to a reader, but folding them here would merge two topics a model
    separated, and a missed merge only costs a second row.
    """
    slug = re.sub(r"[\W_]+", "-", topic.casefold()).strip("-")
    return slug[:60] or "decision"


def decision_key(source_message_id: int, topic: str) -> str:
    """Identity for an extracted decision.

    Derived from the source message and the topic, never from the summary:
    a model rephrases its summary between runs, and a key over generated text
    would record the same decision again on every pass.
    """
    return f"{source_message_id}:{topic_slug(topic)}"
