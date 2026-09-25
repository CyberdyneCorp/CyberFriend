"""Turning candidate messages into recorded asks.

Extraction runs here, on ingest, rather than when somebody asks a question.
Extracting at query time repeats identical work on every question, costs a
full model run each time, and can only see whatever retrieval happened to
surface -- which is how "what did people ask me today" quietly misses things.

Decisions ride the same call. The model that reads a candidate for asks also
reports the decision it concludes, and they are written here through their own
store, in their own transaction: a decision that fails to store must not cost
the asks from the same message, nor the other way round.
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
from chatmemory.app.decisions.model import Decision, ExtractedDecision, decision_key
from chatmemory.app.decisions.ports import DecisionStore
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
    decisions: int = 0
    decisions_failed: int = 0


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
        decisions: DecisionStore | None = None,
    ) -> None:
        self._extractor = extractor
        self._store = store
        # Optional so the ask pipeline stands on its own in tests that are not
        # about decisions; production always passes one.
        self._decisions = decisions
        self._directory = directory
        self._candidates = candidates or CandidateFilter()
        self._policy = policy or AskPolicy()
        self._names = names or {}

    async def extract_window(
        self,
        messages: Sequence[Message],
        parents: Mapping[int, Message] | None = None,
        preceding: Mapping[int, Sequence[Message]] | None = None,
    ) -> ExtractionReport:
        """Extract from one window's messages and record what was found.

        `preceding` is `CandidateFilter.candidates`'s: the corpus's own
        conversation before a message, for a batch that is not contiguous.
        """
        report = ExtractionReport()
        candidates = self._candidates.candidates(messages, parents, preceding)
        for candidate in candidates:
            report = _add(report, await self.extract_candidate(candidate))
        read = {c.message.platform_message_id for c in candidates}
        skipped = [m.platform_message_id for m in messages if m.platform_message_id not in read]
        return _add(report, await self._withdraw_decisions(skipped))

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

        asks = [self._to_ask(candidate, item) for item in extracted.asks]
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
        report = ExtractionReport(
            candidates=1,
            extracted=len(unique),
            recorded=recorded,
            unattributed=sum(1 for a in unique if not a.addressee.is_attributed),
            below_threshold=sum(
                1 for a in unique if not self._policy.presentable(a.confidence)
            ),
        )
        return _add(report, await self._record_decisions(candidate, extracted.decisions))

    async def _record_decisions(
        self, candidate: AskCandidate, extracted: Sequence[ExtractedDecision]
    ) -> ExtractionReport:
        """Replace the message's decisions, even with none.

        Written when nothing was found as well: that write is the prune which
        withdraws a decision an edit took back.
        """
        if self._decisions is None:
            return ExtractionReport()
        found = [self._to_decision(candidate, item) for item in extracted]
        unique = list({d.key: d for d in found}.values())
        source = candidate.message.platform_message_id
        try:
            recorded = await self._decisions.record_decisions(source, unique)
        except Exception:  # noqa: BLE001 - the asks from this message are already stored
            log.exception("decisions.record_failed", message_id=source)
            return ExtractionReport(decisions_failed=1)
        return ExtractionReport(decisions=recorded)

    async def _withdraw_decisions(self, message_ids: Sequence[int]) -> ExtractionReport:
        """Drop decisions from messages this pass read but did not send to the model."""
        if self._decisions is None or not message_ids:
            return ExtractionReport()
        try:
            await self._decisions.withdraw(message_ids)
        except Exception:  # noqa: BLE001 - a stale decision is retried on the next edit
            log.exception("decisions.withdraw_failed", count=len(message_ids))
            return ExtractionReport(decisions_failed=1)
        return ExtractionReport()

    def _to_decision(self, candidate: AskCandidate, extracted: ExtractedDecision) -> Decision:
        message = candidate.message
        return Decision(
            key=decision_key(message.platform_message_id, extracted.topic),
            source_message_id=message.platform_message_id,
            channel=message.channel,
            author=message.author,
            summary=extracted.summary,
            topic=extracted.topic,
            confidence=extracted.confidence,
            decided_at=message.created_at,
            evidence_message_ids=_evidence(candidate),
            thread_id=message.thread_id,
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


def _evidence(candidate: AskCandidate) -> tuple[int, ...]:
    """Every message the model was shown: the source first, then its context.

    The summary may quote any of them, so each one's deletion or opt-out has
    to be able to reach the decision.
    """
    shown = [candidate.message, *candidate.context]
    if candidate.reply_parent is not None:
        shown.append(candidate.reply_parent)
    return tuple(dict.fromkeys(m.platform_message_id for m in shown))


def _add(total: ExtractionReport, one: ExtractionReport) -> ExtractionReport:
    return ExtractionReport(
        candidates=total.candidates + one.candidates,
        extracted=total.extracted + one.extracted,
        recorded=total.recorded + one.recorded,
        unattributed=total.unattributed + one.unattributed,
        below_threshold=total.below_threshold + one.below_threshold,
        failed=total.failed + one.failed,
        decisions=total.decisions + one.decisions,
        decisions_failed=total.decisions_failed + one.decisions_failed,
    )
