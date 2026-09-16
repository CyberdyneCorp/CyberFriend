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

from collections.abc import Mapping
from dataclasses import dataclass, replace
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
    ToolPermit,
)
from chatmemory.app.egress import (
    EgressGuard,
    EgressRefused,
    EgressRequest,
    ProvenancedQuery,
    QueryOrigin,
    RefusalReason,
    authorized,
    keep_asker_words,
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
        egress: EgressGuard | None = None,
    ) -> None:
        self._federation = federation
        self._authorizer = authorizer
        self._audit = audit
        self._confirmations = confirmations
        self._egress = egress or EgressGuard()

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
        # Clearance is minted here, from the question on the request, and
        # made current only for the length of this dispatch. This is the one
        # place that knows both the asking person's words and the provider
        # about to be called; a provider reached any other way finds no
        # clearance and refuses, which is what keeps the boundary unskippable
        # rather than merely documented.
        arguments: Mapping[str, object] = request.arguments
        try:
            try:
                clearance = self._egress.authorize(
                    EgressRequest(
                        asker=request.requester,
                        query=_query_for(request, permit),
                        provider=permit.server,
                    )
                )
            except EgressRefused as refused:
                # A read-only reformulation carrying words the asker did not
                # write gets one more chance, with those words removed. Only
                # for NOT_ROOTED: content-derived text is refused outright,
                # because trimming it would still send what content chose.
                if (
                    refused.reason is not RefusalReason.NOT_ROOTED_IN_QUESTION
                    or permit.effect.mutates
                ):
                    raise
                rooted = _rooted_arguments(request)
                if rooted is None:
                    raise
                arguments = rooted
                trimmed = replace(request, arguments=rooted)
                clearance = self._egress.authorize(
                    EgressRequest(
                        asker=request.requester,
                        query=_query_for(trimmed, permit),
                        provider=permit.server,
                    )
                )
                log.info(
                    "federation.query_trimmed_to_asker_words",
                    tool=request.qualified_name,
                    requester=str(request.requester),
                )
        except EgressRefused as refused:
            log.warning(
                "federation.egress_refused",
                tool=request.qualified_name,
                requester=str(request.requester),
                reason=str(refused.reason),
            )
            # Refused after approval: the attempt happened, so the approval
            # is spent. Otherwise a refusal leaves the "yes" reusable.
            self._confirmations.consume(request)
            entry = self._audit.append(request, decision, AuditOutcome.REFUSED, now=now)
            return InvocationOutcome(decision=decision, entry=entry)

        with authorized(clearance):
            result = await self._federation.call(permit, arguments)

        # Consumed whatever the outcome. An approval authorises one attempt,
        # not one success: leaving it granted after a timeout or a transport
        # error left a live "yes" in the ledger for the rest of its window,
        # which a second call with the same arguments could spend without
        # asking anyone. The person approved a call that has now been made.
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


def _rooted_arguments(request: InvocationRequest) -> Mapping[str, object] | None:
    """The arguments with every word the asker did not write removed.

    None when trimming leaves a string argument empty: a call whose query was
    entirely the model's own words has nothing of the asker's left to send,
    and sending an empty query is not a smaller version of the same call.
    """
    trimmed: dict[str, object] = {}
    for key, value in request.arguments.items():
        if isinstance(value, str) and value.strip():
            kept = keep_asker_words(value, request.question)
            if not kept:
                return None
            trimmed[key] = kept
        else:
            trimmed[key] = value
    return trimmed


def _query_for(request: InvocationRequest, permit: ToolPermit) -> ProvenancedQuery:
    """The query this call would send, with where its text came from.

    A tool's arguments are written by the model, so the text it proposes is
    a reformulation at best: it is admitted only if every word is one the
    asker wrote. The question is taken from the request rather than from the
    arguments, because the arguments are the thing being checked.
    """
    # A mutating call has already been shown to the requester, argument for
    # argument, and invoked only because they approved it. Rooting its text
    # in their question would refuse every legitimate action -- nobody types
    # the body of the comment they are asking the agent to post -- and the
    # human who read the exact arguments is a stronger check than word
    # containment. Rooting is for calls made WITHOUT that review.
    if permit.effect.mutates:
        return ProvenancedQuery(
            text=request.question, origin=QueryOrigin.ASKER, question=request.question
        )

    # EVERY string argument, not one named key. Checking only `query` left
    # every other field unexamined, so a call carrying private text in `q`,
    # `context` or any name the model chose went out unread -- and when
    # `query` was absent the check compared the question against itself and
    # passed trivially. What is checked has to be what is sent.
    texts = tuple(
        value.strip()
        for value in request.arguments.values()
        if isinstance(value, str) and value.strip()
    )
    if not texts:
        # Nothing textual leaves, so there is nothing to root. Recorded
        # against the question so the audit line still names the run.
        return ProvenancedQuery(
            text=request.question, origin=QueryOrigin.ASKER, question=request.question
        )

    combined = " ".join(texts)
    return ProvenancedQuery(
        text=combined,
        origin=(
            QueryOrigin.ASKER
            if len(texts) == 1 and texts[0] == request.question.strip()
            else QueryOrigin.MODEL_REFORMULATION
        ),
        question=request.question,
    )
