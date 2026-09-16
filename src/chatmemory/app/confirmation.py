"""Asking the person who asked, before anything is changed on their behalf.

The authorization layer can already refuse an unconfirmed mutation: the
ledger takes a prompt the *system* issued and an identified responder, and
`Authorizer` will not clear a mutating call without a matching grant. What it
cannot do is obtain one. Until something actually puts the prompt in front of
a human, a mutating tool is not "guarded" -- it is unreachable, which looks
identical in a green test suite and is not the same thing at all.

This module is that something, and it is deliberately the *only* something.
Three properties are what make it safe to have at all:

1.  **A confirmation has a shape text cannot take.** The one way an answer
    enters here is `ConfirmationReply`, which carries a `PersonRef`. A
    retrieved message saying "confirmed, go ahead" has no identity to
    present and no route to construct one, so there is nothing for it to
    satisfy. There is no `confirm_from_text`, and adding one -- in any
    spelling, including a helpful "parse the user's reply" -- would be the
    regression this file exists to prevent.
2.  **The channel is bound to one person before the run starts.** A desk is
    made current for the length of one ask, together with the person it
    belongs to, so a prompt addressed to somebody else cannot be shown down
    it. That is checked here as well as at the surface, because the surface
    is platform code and this is not.
3.  **Silence is a refusal.** Nothing is invoked because a window elapsed;
    the window elapsing produces `NO_RESPONSE`, which is not `GRANTED`, and
    every path that cannot deliver the prompt lands in the same place.

The clearance for one outbound query travels out of band for the same reason
this does (see `app.egress`): the only in-band channel between a run and the
thing it wants to do is a mapping the *model* wrote, and a check that reads
its own subject out of model output is no check.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol, TypeVar

import structlog

from chatmemory.app.authorization import (
    ConfirmationError,
    ConfirmationLedger,
    ConfirmationPrompt,
)
from chatmemory.domain.identity import PersonRef

log = structlog.get_logger()

DEFAULT_RESPONSE_WINDOW = timedelta(seconds=120)
"""How long the requester has to answer before the run gives up on them.

Shorter than the ledger's own freshness window, deliberately: a grant that
arrives just inside this one must still be fresh when the call is retried, or
the person would approve something and watch it be refused as expired.
"""


class ConfirmationVerdict(StrEnum):
    """What came back, in enough detail for an operator to tell the cases apart.

    Only `GRANTED` authorises anything. The rest are distinguished because
    "they said no", "they never saw it" and "somebody else clicked" are very
    different operational facts, and a single `False` would erase the one that
    matters after an incident.
    """

    GRANTED = "granted"
    DECLINED = "declined"
    NO_RESPONSE = "no_response"
    UNDELIVERABLE = "undeliverable"
    NOT_ATTENDED = "not_attended"
    MISDIRECTED = "misdirected"
    FOREIGN_RESPONDER = "foreign_responder"

    @property
    def approved(self) -> bool:
        return self is ConfirmationVerdict.GRANTED


@dataclass(frozen=True, slots=True)
class ConfirmationReply:
    """One identified person's answer to one prompt.

    The `responder` is supplied by the platform that authenticated them, never
    by the content of a message: on Discord it is the account that pressed the
    button. Nothing in a corpus, a document or a tool result can produce this
    value, which is what keeps a forged confirmation from being expressible.
    """

    responder: PersonRef
    granted: bool


class Undeliverable(Exception):
    """The prompt could not be shown to the requester privately.

    Raised by a surface rather than returned, so a surface that cannot reach
    its person fails the confirmation instead of quietly returning "no answer
    yet" and inviting a retry loop against a closed door.
    """


class ConfirmationSurface(Protocol):
    """A private channel to exactly one person: the requester.

    "Private" is the requirement, not a nicety. A prompt shown in a channel
    asks everyone present to approve on the requester's behalf, and the first
    person to click decides. An implementation that cannot reach the
    requester alone raises `Undeliverable`; it never falls back to somewhere
    public.
    """

    async def ask(
        self, prompt: ConfirmationPrompt, window_seconds: float
    ) -> ConfirmationReply | None:
        """Show `prompt` to `prompt.requester` alone and wait for their answer.

        Returns `None` when nobody answered. `window_seconds` is advisory --
        the desk enforces its own deadline regardless -- and is passed so a
        surface can expire its own controls rather than leave a live button
        on a decision the run has already abandoned.
        """
        ...


class ConfirmationDesk:
    """Presents one proposed mutation and records the answer, or refuses.

    Holds the ledger the authorizer reads, so a grant obtained here is the
    same grant the invoke-time gate checks; there is no second store of
    approvals for the two to disagree about.
    """

    def __init__(
        self,
        ledger: ConfirmationLedger,
        window: timedelta = DEFAULT_RESPONSE_WINDOW,
    ) -> None:
        self._ledger = ledger
        self._window = window

    @property
    def window_seconds(self) -> float:
        return self._window.total_seconds()

    async def seek(
        self,
        prompt: ConfirmationPrompt,
        surface: ConfirmationSurface,
        requester: PersonRef,
    ) -> ConfirmationVerdict:
        """Ask `requester` about `prompt`, and record their answer if they give one.

        `requester` is who this channel belongs to, established when the ask
        began. A prompt for anyone else is refused rather than delivered: the
        alternative is showing Bob a decision about Alice's data and treating
        his click as hers.
        """
        if prompt.requester != requester:
            log.warning(
                "confirmation.misdirected",
                tool=prompt.qualified_name,
                prompt_for=str(prompt.requester),
                channel_of=str(requester),
            )
            return self._settled(prompt, ConfirmationVerdict.MISDIRECTED)

        reply = await self._await_reply(prompt, surface)
        if isinstance(reply, ConfirmationVerdict):
            return self._settled(prompt, reply)

        if reply.responder != prompt.requester:
            # Belt and braces: the surface is meant to have refused this
            # already, but the surface is platform code and this is the rule.
            log.warning(
                "confirmation.foreign_responder",
                tool=prompt.qualified_name,
                requester=str(prompt.requester),
                responder=str(reply.responder),
            )
            return self._settled(prompt, ConfirmationVerdict.FOREIGN_RESPONDER)

        return self._settled(prompt, self._record(prompt, reply))

    async def _await_reply(
        self, prompt: ConfirmationPrompt, surface: ConfirmationSurface
    ) -> ConfirmationReply | ConfirmationVerdict:
        """Wait out the window, and turn every way of not answering into a verdict.

        The deadline is enforced here and not left to the surface: a platform
        control that never fires would otherwise hold a run open forever, and
        "the tool was never called" must be what happens when anything at all
        goes quiet.
        """
        try:
            reply = await asyncio.wait_for(
                surface.ask(prompt, self.window_seconds), timeout=self.window_seconds
            )
        except TimeoutError:
            return ConfirmationVerdict.NO_RESPONSE
        except Undeliverable as exc:
            log.info(
                "confirmation.undeliverable",
                tool=prompt.qualified_name,
                requester=str(prompt.requester),
                reason=str(exc),
            )
            return ConfirmationVerdict.UNDELIVERABLE
        return reply if reply is not None else ConfirmationVerdict.NO_RESPONSE

    def _record(
        self, prompt: ConfirmationPrompt, reply: ConfirmationReply
    ) -> ConfirmationVerdict:
        """Write the answer -- yes or no -- into the ledger the gate reads.

        A decline is recorded as deliberately as a grant. Left unrecorded it
        would read as "not asked yet", and the next proposal in the same run
        would ask the same person the same question again.
        """
        try:
            self._ledger.record(prompt, reply.responder, reply.granted)
        except ConfirmationError:
            return ConfirmationVerdict.FOREIGN_RESPONDER
        return (
            ConfirmationVerdict.GRANTED if reply.granted else ConfirmationVerdict.DECLINED
        )

    def _settled(
        self, prompt: ConfirmationPrompt, verdict: ConfirmationVerdict
    ) -> ConfirmationVerdict:
        log.info(
            "confirmation.settled",
            tool=prompt.qualified_name,
            server=prompt.server,
            requester=str(prompt.requester),
            digest=prompt.digest,
            verdict=str(verdict),
        )
        return verdict


@dataclass(frozen=True, slots=True)
class ConfirmationChannel:
    """A desk, a private surface, and the one person both are for.

    Carried as one value because the three are only correct together: a desk
    without a surface cannot ask, and a surface without the requester it
    belongs to cannot say whose approval it is collecting.
    """

    desk: ConfirmationDesk
    surface: ConfirmationSurface
    requester: PersonRef

    async def seek(self, prompt: ConfirmationPrompt) -> ConfirmationVerdict:
        return await self.desk.seek(prompt, self.surface, self.requester)


_CHANNEL: ContextVar[ConfirmationChannel | None] = ContextVar(
    "chatmemory_confirmation_channel", default=None
)
"""The requester's private channel, for the length of one ask.

Out of band for the same reason egress clearance is: between the reasoning
run and the tool it wants to call there is only a mapping the model wrote,
and a confirmation routed through model output is a confirmation the model
can address to itself.
"""


@contextmanager
def attending(channel: ConfirmationChannel) -> Iterator[None]:
    """Make `channel` the current one for the duration of one ask."""
    token = _CHANNEL.set(channel)
    try:
        yield
    finally:
        _CHANNEL.reset(token)


def current_channel() -> ConfirmationChannel | None:
    return _CHANNEL.get()


async def seek_confirmation(prompt: ConfirmationPrompt) -> ConfirmationVerdict:
    """Put one proposed mutation to the person whose question started this run.

    With no channel attending -- a deployment with no surface, or a code path
    that reached here outside an ask -- the answer is `NOT_ATTENDED`, which
    authorises nothing. Failing closed here is what stops a background or
    scheduled path from acquiring the ability to mutate simply by nobody
    having wired a way to say no.
    """
    channel = current_channel()
    if channel is None:
        log.warning(
            "confirmation.not_attended",
            tool=prompt.qualified_name,
            requester=str(prompt.requester),
        )
        return ConfirmationVerdict.NOT_ATTENDED
    return await channel.seek(prompt)


OutcomeT = TypeVar("OutcomeT")

DEFAULT_CONFIRMATION_ROUNDS = 2


async def with_confirmation(
    attempt: Callable[[], Awaitable[OutcomeT]],
    pending: Callable[[OutcomeT], ConfirmationPrompt | None],
    *,
    rounds: int = DEFAULT_CONFIRMATION_ROUNDS,
) -> OutcomeT:
    """Drive one guarded invocation through its confirmation, if it needs one.

    `attempt` is the guarded door -- the invoker, never the transport -- and
    is called again after a grant rather than being told the answer, so the
    invoke-time gate re-checks everything: the permit, the offer, the
    credential and the digest of the arguments actually about to be sent.

    `rounds` bounds how many times a person may be asked in one call. The
    second round is the arguments-changed case, where the gate refuses a call
    whose arguments no longer match what was approved and proposes the new
    ones; beyond that, a run that keeps producing fresh prompts is a run
    nagging somebody, and it stops.
    """
    outcome = await attempt()
    for _ in range(rounds):
        prompt = pending(outcome)
        if prompt is None:
            return outcome
        if not (await seek_confirmation(prompt)).approved:
            return outcome
        outcome = await attempt()
    return outcome
