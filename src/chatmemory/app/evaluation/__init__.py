"""Measurement instruments for retrieval quality.

Nothing in here runs in the bot or the ingest process. It exists so that the
question "does retrieval find the *right* messages" has an answer that is a
number rather than an opinion, and so that number is computed by code that is
itself tested rather than by a script somebody ran once.
"""

from chatmemory.app.evaluation.scoring import (
    AclBreach,
    GoldenReport,
    Judgement,
    Provenance,
    QuestionScore,
    RankedResult,
    acl_breaches,
    score_question,
)

__all__ = [
    "AclBreach",
    "GoldenReport",
    "Judgement",
    "Provenance",
    "QuestionScore",
    "RankedResult",
    "acl_breaches",
    "score_question",
]
