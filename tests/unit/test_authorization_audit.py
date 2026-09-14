"""The audit trail: append-only in shape, tamper-evident in content."""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from chatmemory.app.audit import (
    GENESIS_DIGEST,
    AuditEntry,
    AuditOutcome,
    AuditTrail,
    InMemoryAuditTrail,
    evidence_summary,
)
from chatmemory.app.authorization import (
    ActionOrigin,
    AuthorizationDecision,
    ConfirmationState,
    ContentKind,
    CredentialScope,
    InvocationRequest,
    Refusal,
    ToolEffect,
    ToolPermit,
    fence_all,
)
from chatmemory.domain.identity import PersonRef

ALICE = PersonRef("discord", 1001)
T0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

WRITE = ToolPermit(
    qualified_name="github:close_issue",
    server="github",
    tool="close_issue",
    effect=ToolEffect.MUTATING,
    credential=CredentialScope.PER_REQUESTER,
    mutation_enabled=True,
)

ALLOWED = AuthorizationDecision(
    allowed=True, confirmation=ConfirmationState.GRANTED, permit=WRITE
)
REFUSED = AuthorizationDecision(
    allowed=False,
    confirmation=ConfirmationState.ABSENT,
    refusal=Refusal.CONFIRMATION_REQUIRED,
    permit=WRITE,
)


def call(
    *, qualified_name: str = "github:close_issue", evidence_bodies: tuple[str, ...] = ()
) -> InvocationRequest:
    evidence = fence_all(
        ContentKind.RETRIEVED_MESSAGE, [("discord:100", b) for b in evidence_bodies]
    )
    return InvocationRequest(
        requester=ALICE,
        question="close the auth bug issue",
        qualified_name=qualified_name,
        arguments={"number": 42},
        origin=ActionOrigin.REQUESTER_REQUEST,
        evidence=evidence,
    )


# --- what is recorded --------------------------------------------------


def test_an_invocation_records_everything_needed_to_reconstruct_it() -> None:
    trail = InMemoryAuditTrail()
    entry = trail.append(
        call(evidence_bodies=("please close it", "agreed")), ALLOWED, AuditOutcome.INVOKED, now=T0
    )

    assert entry.requester == "discord:1001"
    assert entry.question == "close the auth bug issue"
    assert (entry.server, entry.tool) == ("github", "close_issue")
    assert entry.arguments == '{"number":42}'
    assert entry.outcome is AuditOutcome.INVOKED
    assert entry.confirmation is ConfirmationState.GRANTED
    assert entry.origin == "requester_request"
    # The evidence in context is identified, so an incident review can see
    # which fenced items were present when the call was proposed.
    assert len(entry.evidence_fence_ids) == 2


def test_a_refusal_is_recorded_with_its_reason() -> None:
    trail = InMemoryAuditTrail()
    entry = trail.append(call(), REFUSED, AuditOutcome.REFUSED, now=T0)
    assert entry.outcome is AuditOutcome.REFUSED
    assert entry.refusal == "confirmation_required"


def test_a_refused_call_to_an_unregistered_tool_still_names_what_was_attempted() -> None:
    """There is no permit to read the server and tool from, and it still records."""
    trail = InMemoryAuditTrail()
    unregistered = AuthorizationDecision(
        allowed=False,
        confirmation=ConfirmationState.NOT_REQUIRED,
        refusal=Refusal.NOT_REGISTERED,
    )
    entry = trail.append(
        call(qualified_name="github:exfiltrate"), unregistered, AuditOutcome.REFUSED, now=T0
    )
    assert (entry.server, entry.tool) == ("github", "exfiltrate")
    assert entry.qualified_name == "github:exfiltrate"


def test_the_arguments_recorded_are_the_bytes_that_were_confirmed() -> None:
    """Same rendering in the prompt and in the trail, so they cannot diverge."""
    from chatmemory.app.authorization import ConfirmationLedger

    ledger = ConfirmationLedger()
    request = call()
    prompt = ledger.propose(request, WRITE, now=T0)
    entry = InMemoryAuditTrail().append(request, ALLOWED, AuditOutcome.INVOKED, now=T0)
    assert entry.arguments == prompt.arguments_rendered
    assert entry.argument_digest == prompt.digest


# --- append-only shape -------------------------------------------------


def test_the_trail_exposes_no_way_to_edit_or_delete() -> None:
    """The guarantee is the missing methods, not a rule someone remembers."""
    for kind in (AuditTrail, InMemoryAuditTrail):
        public = {n for n in dir(kind) if not n.startswith("_")}
        assert public == {"append", "entries", "verify"}, kind


def test_append_is_the_only_method_that_takes_a_write() -> None:
    signature = inspect.signature(InMemoryAuditTrail.entries)
    assert list(signature.parameters) == ["self"]


def test_entries_are_returned_as_an_immutable_snapshot() -> None:
    trail = InMemoryAuditTrail()
    trail.append(call(), ALLOWED, AuditOutcome.INVOKED, now=T0)
    snapshot = trail.entries()
    assert isinstance(snapshot, tuple)
    with pytest.raises(AttributeError):
        snapshot.append(None)  # type: ignore[attr-defined]
    # And the entries themselves are frozen.
    with pytest.raises(AttributeError):
        snapshot[0].outcome = AuditOutcome.REFUSED  # type: ignore[misc]


def test_the_snapshot_does_not_alias_the_trail() -> None:
    trail = InMemoryAuditTrail()
    trail.append(call(), ALLOWED, AuditOutcome.INVOKED, now=T0)
    snapshot = trail.entries()
    trail.append(call(), REFUSED, AuditOutcome.REFUSED, now=T0)
    assert len(snapshot) == 1
    assert len(trail.entries()) == 2


# --- tamper evidence ---------------------------------------------------


def test_a_fresh_trail_verifies() -> None:
    trail = InMemoryAuditTrail()
    assert trail.verify()
    trail.append(call(), ALLOWED, AuditOutcome.INVOKED, now=T0)
    trail.append(call(), REFUSED, AuditOutcome.REFUSED, now=T0)
    assert trail.verify()


def test_the_first_entry_anchors_to_a_known_digest() -> None:
    trail = InMemoryAuditTrail()
    first = trail.append(call(), ALLOWED, AuditOutcome.INVOKED, now=T0)
    assert first.previous_digest == GENESIS_DIGEST
    assert first.sequence == 0


def test_entries_chain_to_their_predecessor() -> None:
    trail = InMemoryAuditTrail()
    first = trail.append(call(), ALLOWED, AuditOutcome.INVOKED, now=T0)
    second = trail.append(call(), REFUSED, AuditOutcome.REFUSED, now=T0)
    assert second.previous_digest == first.digest


def test_editing_an_entry_is_detected() -> None:
    """Storage cannot stop a process with write access; it can expose one."""
    trail = InMemoryAuditTrail()
    trail.append(call(), REFUSED, AuditOutcome.REFUSED, now=T0)
    trail.append(call(), ALLOWED, AuditOutcome.INVOKED, now=T0)

    stored: list[AuditEntry] = trail._entries  # noqa: SLF001 - simulating a tamper
    stored[0] = replace(stored[0], outcome=AuditOutcome.INVOKED)
    assert not trail.verify()


def test_removing_an_entry_is_detected() -> None:
    trail = InMemoryAuditTrail()
    trail.append(call(), REFUSED, AuditOutcome.REFUSED, now=T0)
    trail.append(call(), ALLOWED, AuditOutcome.INVOKED, now=T0)
    trail.append(call(), ALLOWED, AuditOutcome.INVOKED, now=T0)

    stored: list[AuditEntry] = trail._entries  # noqa: SLF001
    del stored[1]
    assert not trail.verify()


def test_rewriting_an_entry_and_resealing_it_still_breaks_the_chain() -> None:
    """Resealing one entry does not reseal its successors."""
    trail = InMemoryAuditTrail()
    trail.append(call(), REFUSED, AuditOutcome.REFUSED, now=T0)
    trail.append(call(), ALLOWED, AuditOutcome.INVOKED, now=T0)

    stored: list[AuditEntry] = trail._entries  # noqa: SLF001
    doctored = replace(stored[0], outcome=AuditOutcome.INVOKED, digest="")
    stored[0] = replace(doctored, digest=doctored.recompute_digest())
    assert stored[0].digest == stored[0].recompute_digest()
    assert not trail.verify()


def test_evidence_summary_reports_what_was_in_context() -> None:
    summary = evidence_summary(call(evidence_bodies=("a", "b")))
    assert summary["item_count"] == 2
    assert summary["origins"] == ["discord:100"]
