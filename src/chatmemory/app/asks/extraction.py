"""Turning candidate messages into recorded asks.

Extraction runs here, on ingest, rather than when somebody asks a question.
Extracting at query time repeats identical work on every question, costs a
full model run each time, and can only see whatever retrieval happened to
surface -- which is how "what did people ask me today" quietly misses things.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import structlog

from chatmemory.app.asks.candidates import CandidateFilter
from chatmemory.app.asks.model import (
    Ask,
    AskCandidate,
    AskPolicy,
    ExtractedAsk,
    ask_key,
)
from chatmemory.app.asks.ports import AskExtractor, AskStore
from chatmemory.app.asks.resolution import PersonDirectory, resolve_addressee
from chatmemory.domain.identity import PersonRef
from chatmemory.domain.messages import Message

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class ExtractionReport:
    candidates: int = 0
    extracted: int = 0
    recorded: int = 0
    unattributed: int = 0
    below_threshold: int = 0
    failed: int = 0


class ExtractionService:
    """Candidate filter, model call, addressee resolution, write."""

    def __init__(
        self,
        extractor: AskExtractor,
        store: AskStore,
        directory: PersonDirectory,
        candidates: CandidateFilter | None = None,
        policy: AskPolicy | None = None,
        names: Mapping[PersonRef, str] | None = None,
    ) -> None:
        self._extractor = extractor
        self._store = store
        self._directory = directory
        self._candidates = candidates or CandidateFilter()
        self._policy = policy or AskPolicy()
        self._names = names or {}

    async def extract_window(
        self,
        messages: Sequence[Message],
        parents: Mapping[int, Message] | None = None,
    ) -> ExtractionReport:
        """Extract from one window's messages and record what was found."""
        report = ExtractionReport()
        for candidate in self._candidates.candidates(messages, parents):
            report = _add(report, await self.extract_candidate(candidate))
        return report

    async def extract_candidate(self, candidate: AskCandidate) -> ExtractionReport:
        try:
            extracted = await self._extractor.extract(candidate)
        except Exception:  # noqa: BLE001 - one unreadable message must not stop ingest
            log.exception(
                "asks.extraction_failed",
                message_id=candidate.message.platform_message_id,
                channel=str(candidate.channel),
            )
            return ExtractionReport(candidates=1, failed=1)

        asks = [self._to_ask(candidate, item) for item in extracted]
        # Deduplicated by key before the write: a model that reports the same
        # obligation twice in one message must not produce two of them.
        unique = list({ask.key: ask for ask in asks}.values())

        # Sub-threshold asks are recorded, not discarded. Storing them is how
        # the threshold gets tuned against reality; the read path is where they
        # are kept out of answers.
        recorded = await self._store.record_asks(
            candidate.message.platform_message_id, unique
        )

        log.debug(
            "asks.extracted",
            message_id=candidate.message.platform_message_id,
            signal=candidate.signal,
            found=len(unique),
            recorded=recorded,
        )
        return ExtractionReport(
            candidates=1,
            extracted=len(unique),
            recorded=recorded,
            unattributed=sum(1 for a in unique if not a.addressee.is_attributed),
            below_threshold=sum(
                1 for a in unique if not self._policy.presentable(a.confidence)
            ),
        )

    def _to_ask(self, candidate: AskCandidate, extracted: ExtractedAsk) -> Ask:
        message = candidate.message
        addressee = resolve_addressee(candidate, extracted, self._directory)
        return Ask(
            key=ask_key(message.platform_message_id, extracted.kind, addressee),
            source_message_id=message.platform_message_id,
            channel=message.channel,
            requester=message.author,
            addressee=addressee,
            kind=extracted.kind,
            text=extracted.text,
            confidence=extracted.confidence,
            asked_at=message.created_at,
            thread_id=message.thread_id,
        )


def _add(total: ExtractionReport, one: ExtractionReport) -> ExtractionReport:
    return ExtractionReport(
        candidates=total.candidates + one.candidates,
        extracted=total.extracted + one.extracted,
        recorded=total.recorded + one.recorded,
        unattributed=total.unattributed + one.unattributed,
        below_threshold=total.below_threshold + one.below_threshold,
        failed=total.failed + one.failed,
    )
