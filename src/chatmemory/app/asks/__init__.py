"""Asks: who asked what of whom, and whether it is still outstanding.

"What did people ask me today" and "what do I need to do today" are questions
about *state*, not about topical resemblance. Embedding them and hoping the
right windows surface is how this feature fails, so obligations are extracted
once as conversation arrives and answered afterwards by lookup.

The pipeline, in order:

    candidates  -> which messages are worth a model call at all
    extraction  -> the model call, against a strict schema
    resolution  -> who the ask fell to, or `UNATTRIBUTED`
    state       -> open, answered or stale, from observed events only
    corrections -> the addressee's own word, which outranks all of the above
    obligations -> the viewer-scoped read path
"""

from __future__ import annotations

from chatmemory.app.asks.corrections import CorrectionService
from chatmemory.app.asks.extraction import ExtractionReport, ExtractionService
from chatmemory.app.asks.model import (
    UNATTRIBUTED,
    Addressee,
    AddresseeKind,
    Ask,
    AskKind,
    AskPolicy,
    AskStatus,
    Correction,
    CorrectionOutcome,
    CorrectionResolution,
    ObligationRequest,
    ReportedAsk,
    ask_key,
)
from chatmemory.app.asks.obligations import ObligationService, discord_message_url
from chatmemory.app.asks.ports import AskExtractor, AskStore
from chatmemory.app.asks.resolution import StaticDirectory
from chatmemory.app.asks.state import AskStateService

__all__ = [
    "UNATTRIBUTED",
    "Addressee",
    "AddresseeKind",
    "Ask",
    "AskExtractor",
    "AskKind",
    "AskPolicy",
    "AskStateService",
    "AskStatus",
    "AskStore",
    "Correction",
    "CorrectionOutcome",
    "CorrectionResolution",
    "CorrectionService",
    "ExtractionReport",
    "ExtractionService",
    "ObligationRequest",
    "ObligationService",
    "ReportedAsk",
    "StaticDirectory",
    "ask_key",
    "discord_message_url",
]
