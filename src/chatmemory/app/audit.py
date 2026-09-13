"""The record of every federated tool invocation, and why it happened.

Two properties matter more than the contents:

*   **Append-only.** The protocol has one write method. There is no update
    and no delete, at any layer, so "a requesting person must not be able to
    alter or remove it" is a shape rather than a rule someone remembers.
*   **Tamper-evident.** Each entry hashes its own fields together with the
    previous entry's digest, so editing or dropping one breaks every digest
    after it. An in-memory or file-backed trail cannot *prevent* a process
    with write access from rewriting history; it can make the rewrite
    detectable, which is the achievable guarantee and the one `verify()`
    checks.

A refusal is recorded exactly like an invocation. The interesting audit
question after an incident is usually "what did it decline to do", and a
trail that only records successes cannot answer it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

import structlog

from chatmemory.app.authorization import (
    AuthorizationDecision,
    ConfirmationState,
    InvocationRequest,
    ToolPermit,
    canonical_arguments,
)

log = structlog.get_logger()

GENESIS_DIGEST = "0" * 64
"""The previous-digest of the first entry. Anchors the chain at a known value."""


class AuditOutcome(StrEnum):
    INVOKED = "invoked"
    REFUSED = "refused"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One immutable line of the trail.

    `arguments` is the same canonical rendering shown to the person who
    confirmed, so an auditor compares the text that was approved against the
    text that was sent rather than two independent formattings of a dict.
    """

    sequence: int
    recorded_at: datetime
    requester: str
    question: str
    server: str
    tool: str
    qualified_name: str
    origin: str
    arguments: str
    argument_digest: str
    evidence_fence_ids: tuple[str, ...]
    outcome: AuditOutcome
    refusal: str | None
    confirmation: ConfirmationState
    previous_digest: str
    digest: str

    def recompute_digest(self) -> str:
        payload = asdict(self)
        payload.pop("digest")
        payload["recorded_at"] = self.recorded_at.isoformat()
        payload["evidence_fence_ids"] = list(self.evidence_fence_ids)
        payload["outcome"] = str(self.outcome)
        payload["confirmation"] = str(self.confirmation)
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class AuditTrail(Protocol):
    """The audit port.

    Read the method list as the guarantee: `append`, `entries`, `verify`. A
    deletion or an edit is not something a caller can express, so no code
    path -- including one a crafted message talks the agent into taking --
    can reach one.
    """

    def append(
        self,
        request: InvocationRequest,
        decision: AuthorizationDecision,
        outcome: AuditOutcome,
        *,
        permit: ToolPermit | None = None,
        now: datetime | None = None,
    ) -> AuditEntry:
        """Record one decision and its result. Returns the entry written."""
        ...

    def entries(self) -> Sequence[AuditEntry]:
        """Every entry, oldest first."""
        ...

    def verify(self) -> bool:
        """Whether the hash chain is intact."""
        ...


class InMemoryAuditTrail:
    """A process-local trail. Durable enough for tests, honest about its limits.

    A deployment wanting non-repudiation past a restart points the same port
    at append-only storage; nothing above this type changes, because nothing
    above it can express anything but an append.
    """

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def append(
        self,
        request: InvocationRequest,
        decision: AuthorizationDecision,
        outcome: AuditOutcome,
        *,
        permit: ToolPermit | None = None,
        now: datetime | None = None,
    ) -> AuditEntry:
        resolved = permit or decision.permit
        previous = self._entries[-1].digest if self._entries else GENESIS_DIGEST
        entry = AuditEntry(
            sequence=len(self._entries),
            recorded_at=now or datetime.now(UTC),
            requester=str(request.requester),
            question=request.question,
            # A refused call may name a tool no server provides, so the server
            # and tool halves come from the permit when there is one and from
            # the name the model emitted when there is not.
            server=resolved.server if resolved else _server_of(request.qualified_name),
            tool=resolved.tool if resolved else _tool_of(request.qualified_name),
            qualified_name=request.qualified_name,
            origin=str(request.origin),
            arguments=canonical_arguments(request.arguments),
            argument_digest=request.digest,
            evidence_fence_ids=request.evidence.fence_ids,
            outcome=outcome,
            refusal=str(decision.refusal) if decision.refusal is not None else None,
            confirmation=decision.confirmation,
            previous_digest=previous,
            digest="",
        )
        sealed = _seal(entry)
        self._entries.append(sealed)
        log.info(
            "audit.recorded",
            sequence=sealed.sequence,
            requester=sealed.requester,
            tool=sealed.qualified_name,
            outcome=str(outcome),
            refusal=sealed.refusal,
            confirmation=str(sealed.confirmation),
        )
        return sealed

    def entries(self) -> Sequence[AuditEntry]:
        # A tuple of frozen dataclasses: a caller holding the result cannot
        # append to it, reorder it, or edit an entry in place.
        return tuple(self._entries)

    def verify(self) -> bool:
        expected = GENESIS_DIGEST
        for index, entry in enumerate(self._entries):
            if entry.sequence != index or entry.previous_digest != expected:
                return False
            if entry.digest != entry.recompute_digest():
                return False
            expected = entry.digest
        return True


def _seal(entry: AuditEntry) -> AuditEntry:
    """Fill in the digest. Separate so the hashed payload excludes it."""
    return replace(entry, digest=entry.recompute_digest())


def _server_of(qualified_name: str) -> str:
    server, _, _ = qualified_name.partition(":")
    return server


def _tool_of(qualified_name: str) -> str:
    _, _, tool = qualified_name.partition(":")
    return tool or qualified_name


def evidence_summary(request: InvocationRequest) -> Mapping[str, object]:
    """What was in context when this call was proposed, for an operator view."""
    return {
        "fence_ids": list(request.evidence.fence_ids),
        "origins": list(request.evidence.origins),
        "item_count": len(request.evidence.items),
    }
