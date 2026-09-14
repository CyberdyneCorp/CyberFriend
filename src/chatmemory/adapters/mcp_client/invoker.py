"""The one door every federated call goes through.

`Federation.call` is transport and `Authorizer.authorize` is policy; this
joins them and is the only thing the reasoning loop is ever handed. That
matters because the two most likely regressions are "someone called the
transport directly" and "the decision was taken when tools were offered
rather than when one was invoked" -- and both become visible as a call site
that does not use this class.

Every path out of `invoke` writes an audit entry. A refusal is as
interesting as an invocation, and more interesting after an incident.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog

from chatmemory.adapters.mcp_client.client import Failure, FederatedResult, Federation
from chatmemory.adapters.mcp_client.routing import RoutedTools
from chatmemory.app.audit import AuditEntry, AuditOutcome, AuditTrail
from chatmemory.app.authorization import (
    AuthorizationDecision,
    Authorizer,
    ConfirmationLedger,
    ConfirmationPrompt,
    FencedContent,
    InvocationRequest,
    Refusal,
)

log = structlog.get_logger()

_NEEDS_CONFIRMATION = frozenset(
    {Refusal.CONFIRMATION_REQUIRED, Refusal.ARGUMENTS_CHANGED, Refusal.CONFIRMATION_EXPIRED}
)


@dataclass(frozen=True, slots=True)
class InvocationOutcome:
    """What happened, and what the surface should do next.

    `prompt` is set exactly when the run is waiting on the requesting person.
    It is not a hint to the model -- the model is not told how to obtain a
    confirmation, because the only way to obtain one is for a person to
    answer, and telling the loop otherwise invites it to try.
    """

    decision: AuthorizationDecision
    entry: AuditEntry
    result: FederatedResult | None = None
    prompt: ConfirmationPrompt | None = None

    @property
    def invoked(self) -> bool:
        return self.result is not None and self.result.ok

    @property
    def awaiting_confirmation(self) -> bool:
        return self.prompt is not None

    def evidence(self) -> FencedContent | None:
        """The result as fenced data, or nothing when there is no result."""
        return self.result.as_evidence() if self.result is not None and self.result.ok else None

    def notice(self) -> str | None:
        return self.result.notice() if self.result is not None else None


_FAILURE_OUTCOMES = {
    Failure.TIMEOUT: AuditOutcome.TIMED_OUT,
    Failure.UNAVAILABLE: AuditOutcome.FAILED,
    Failure.TOOL_ERROR: AuditOutcome.FAILED,
}


class GuardedInvoker:
    """Authorize, invoke, record. In that order, for every call."""

    def __init__(
        self,
        federation: Federation,
        authorizer: Authorizer,
        audit: AuditTrail,
        confirmations: ConfirmationLedger,
    ) -> None:
        self._federation = federation
        self._authorizer = authorizer
        self._audit = audit
        self._confirmations = confirmations

    async def invoke(
        self,
        request: InvocationRequest,
        routed: RoutedTools,
        *,
        now: datetime | None = None,
    ) -> InvocationOutcome:
        decision = self._authorizer.authorize(request, routed.names, now=now)

        if not decision.allowed:
            entry = self._audit.append(request, decision, AuditOutcome.REFUSED, now=now)
            prompt = self._propose(request, decision, now)
            return InvocationOutcome(decision=decision, entry=entry, prompt=prompt)

        permit = decision.permit
        if permit is None:
            # Unreachable: an allowed decision always carries the permit it was
            # allowed against. Raising beats an assert, which -O would remove
            # from exactly the code path that must never run unchecked.
            raise RuntimeError(f"authorized {request.qualified_name} with no permit")
        result = await self._federation.call(permit, request.arguments)

        if result.ok:
            # One approval authorises one call: a second invocation with the
            # same arguments has to be confirmed again rather than replaying
            # the first "yes".
            self._confirmations.consume(request)

        outcome = (
            AuditOutcome.INVOKED
            if result.ok
            else _FAILURE_OUTCOMES.get(result.failure or Failure.UNAVAILABLE, AuditOutcome.FAILED)
        )
        entry = self._audit.append(request, decision, outcome, permit=permit, now=now)
        log.info(
            "federation.invoked",
            tool=request.qualified_name,
            requester=str(request.requester),
            ok=result.ok,
            failure=str(result.failure) if result.failure else None,
            truncated=result.truncated,
        )
        return InvocationOutcome(decision=decision, entry=entry, result=result)

    def _propose(
        self,
        request: InvocationRequest,
        decision: AuthorizationDecision,
        now: datetime | None,
    ) -> ConfirmationPrompt | None:
        """Ask the requester, when asking is what stands in the way.

        Only for refusals a person can resolve. A tool that was never
        registered, or one whose mutations an operator has not enabled, is
        not something the requester may approve their way past -- offering
        them a prompt there would train people to click through the one
        prompt that matters.
        """
        if decision.refusal not in _NEEDS_CONFIRMATION or decision.permit is None:
            return None
        return self._confirmations.propose(request, decision.permit, now=now)
