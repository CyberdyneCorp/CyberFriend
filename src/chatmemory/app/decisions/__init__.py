"""Decisions: what a group settled on, recorded where it was said.

"What did we decide about the deploy?" is a question about a conclusion, and a
conclusion is one line in a long thread -- similarity search over windows finds
the thread and leaves the reader to find the line. So decisions are extracted
once, in the same model call that reads a message for asks, and kept as rows
that cite the message they came from.

This package holds the vocabulary, the ports and the answer. Extraction is the
ask pass's (`app/asks/extraction.py`); the store is
`adapters/store/decisions_postgres.py`; "what did we decide about Y?" is
`answering.py`.
"""

from __future__ import annotations

from chatmemory.app.decisions.model import (
    Decision,
    DecisionPolicy,
    DecisionRequest,
    ExtractedDecision,
    ReportedDecision,
    decision_key,
    search_terms,
    topic_slug,
)
from chatmemory.app.decisions.ports import DecisionSearch, DecisionStore

__all__ = [
    "Decision",
    "DecisionPolicy",
    "DecisionRequest",
    "DecisionSearch",
    "DecisionStore",
    "ExtractedDecision",
    "ReportedDecision",
    "decision_key",
    "search_terms",
    "topic_slug",
]
