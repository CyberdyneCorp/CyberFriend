"""What the critic is allowed to say.

The evaluator returns one of these members and a score -- never prose. A
critic that emits free-form text leaves nothing to route on, and worse, its
text is model output being read as an instruction by the next stage. An
enumerated verdict makes the corrective policy a pure function over a finite
domain, which is why the policy tests need no model in the room.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    """The complete set of judgements the evaluator may return."""

    SUFFICIENT = "sufficient"
    """The evidence answers the question."""

    PARTIAL = "partial"
    """Some of the question is covered. Partial evidence scores high -- it
    genuinely answers part -- which is why a gate calibrated against
    uncovered data waves it through as complete."""

    IRRELEVANT = "irrelevant"
    """Results came back and none of them bear on the question."""

    EMPTY = "empty"
    """Retrieval returned nothing at all."""

    AMBIGUOUS = "ambiguous"
    """The question admits several readings; the evidence cannot settle it."""

    UNANSWERABLE = "unanswerable"
    """The corpus cannot answer this, and retrying will not change that."""


@dataclass(frozen=True, slots=True)
class Assessment:
    """A verdict, its score, and what it cost.

    `model_calls` is zero when a cheap signal reached the verdict -- an empty
    result set, or the relevance gate. Provenance is recorded from this
    number, so it is carried on the assessment rather than inferred later.
    """

    verdict: Verdict
    score: float = 0.0
    model_calls: int = 0
    prompt_tokens: int = 0
    suggested_query: str | None = None
    completion_tokens: int = 0
    model: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"assessment score must be in [0, 1], got {self.score}")
        if self.model_calls < 0:
            raise ValueError("model_calls cannot be negative")
