"""Requesting a CyberdyneAuth account for a person, and the records it leaves.

Two ports:

*   `AccountProvisioner` is the only way anything reaches the identity
    provider. `ProvisioningRequest` is a typed record of exactly `email`,
    `name` and `locale`, so no other fact -- a phone, an address, a wallet --
    can be passed along by accident. The approved contract always answers 202
    with no account id and no "exists" flag, so `request_account` returns
    nothing: there is nothing to learn from it, and so nothing to leak.

*   `AccountStore` keeps what the assistant records about it: consent, the
    requests counted against the per-person limits, the single-use link
    codes, and the link from a CyberdyneAuth subject to a person. Emails only
    ever arrive here as `email_hmac`, or masked as `email_hint`; there is no
    parameter an address could be stored through.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from chatmemory.domain.identity import PersonRef


@dataclass(frozen=True, slots=True)
class ProvisioningRequest:
    """Everything that is ever sent to the provider, and nothing else."""

    email: str
    name: str | None = None
    locale: str | None = None


class ProvisioningFailed(Exception):
    """The provider did not accept the request. Nothing is counted."""


class ProvisioningRateLimited(ProvisioningFailed):
    """The provider's per-client limit (429)."""


class ProvisioningUnavailable(ProvisioningFailed):
    """Any other answer than 202, or no answer."""


class ProvisioningInvalidName(ProvisioningFailed):
    """The provider refused the name (422).

    `account_name` applies CyberdyneAuth's rule before anything is shown, so
    this means the two rules have drifted apart: an operator's problem, not
    the person's, and retrying cannot fix it.
    """


class AccountProvisioner(Protocol):
    async def request_account(self, request: ProvisioningRequest) -> None:
        """Ask the provider to create (or re-invite) an account for the email.

        Returns on 202 whatever happened there: created, already existed, or
        silently throttled. Raises `ProvisioningRateLimited` on 429,
        `ProvisioningInvalidName` on 422 and `ProvisioningUnavailable` on
        anything else.
        """
        ...


@dataclass(frozen=True, slots=True)
class ProvisioningLimits:
    """How many requests one person may make, per rolling window."""

    per_day: int = 1
    per_month: int = 3
    day: timedelta = timedelta(hours=24)
    month: timedelta = timedelta(days=30)


def retry_at(
    earlier: Sequence[datetime], now: datetime, limits: ProvisioningLimits
) -> datetime | None:
    """When the next request is allowed, or None if it is allowed now.

    `earlier` is the person's requests within the last `limits.month`. A
    request is allowed when fewer than `per_day` fall in the last day and
    fewer than `per_month` in the last month; otherwise the answer is when
    enough of them age out of both windows.
    """
    times = sorted(t for t in earlier if t > now - limits.month)
    waits: list[datetime] = []
    today = [t for t in times if t > now - limits.day]
    if len(today) >= limits.per_day:
        waits.append(today[len(today) - limits.per_day] + limits.day)
    if len(times) >= limits.per_month:
        waits.append(times[len(times) - limits.per_month] + limits.month)
    return max(waits) if waits else None


@dataclass(frozen=True, slots=True)
class Reservation:
    """A request counted before the provider is called, or when to come back."""

    request_id: int | None
    retry_at: datetime | None = None

    @property
    def granted(self) -> bool:
        return self.request_id is not None


class LinkCodeVerdict(StrEnum):
    ISSUED = "issued"
    NO_CONSENT = "no_consent"
    """Nothing to link to: the person never confirmed an account request."""
    LIMITED = "limited"


@dataclass(frozen=True, slots=True)
class LinkCodeIssue:
    verdict: LinkCodeVerdict
    retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RedeemedCode:
    """Whose code it was, and which consented email it may be linked to."""

    person_id: int
    email_hmac: bytes


class LinkOutcome(StrEnum):
    """What redeeming a link code after sign-in came to."""

    LINKED = "linked"
    BAD_CODE = "bad_code"
    """Unknown, used, expired or superseded."""
    OTHER_EMAIL = "other_email"
    """The verified email is not the one the person consented with."""
    SUBJECT_TAKEN = "subject_taken"
    """This CyberdyneAuth account is already linked to another person."""


@dataclass(frozen=True, slots=True)
class NewLink:
    """A link to make, once the sign-in that proves it has been verified."""

    code_sha256: bytes
    email_hmac: bytes
    """Of the signed-in, verified email: compared with the code's."""
    issuer: str
    sub: str
    email_hint: str
    """The masked address the bot names when it announces the link."""


@dataclass(frozen=True, slots=True)
class LinkNotice:
    """A link made that the person has not been told about yet."""

    person: PersonRef
    sub: str
    email_hint: str


@dataclass(frozen=True, slots=True)
class DiscordProfile:
    """Which Discord account a code or a link belongs to, as the web names it.

    `/link` names it before anyone signs in, so whoever follows a link sees
    which Discord account they are about to link to; `/me` names it after, so
    an account linked to the wrong person can tell and unlink.
    """

    person: PersonRef
    display_name: str


@dataclass(frozen=True, slots=True)
class AccountCleanup:
    requests: int = 0
    codes: int = 0


class AccountStore(Protocol):
    async def last_requests(self, person: PersonRef, since: datetime) -> Sequence[datetime]:
        """When this person's counted requests were made, from `since` on."""
        ...

    async def reserve(
        self,
        person: PersonRef,
        email_hmac: bytes,
        consent_version: int,
        *,
        now: datetime,
        limits: ProvisioningLimits,
    ) -> Reservation:
        """Record consent and count a request, if the limits allow one now.

        Decided under a lock on the person, so two presses cannot both pass.
        Over a limit nothing is written, and the reservation says when to come
        back.
        """
        ...

    async def release(self, request_id: int) -> None:
        """Uncount a request the provider did not accept."""
        ...

    async def issue_link_code(
        self,
        person: PersonRef,
        code_sha256: bytes,
        *,
        now: datetime,
        ttl: timedelta,
        per_day: int,
    ) -> LinkCodeIssue:
        """Store a new code for the person's latest consented email.

        Every earlier unused code of theirs stops working. At most `per_day`
        codes in any 24 hours.
        """
        ...

    async def redeem_link_code(self, code_sha256: bytes, now: datetime) -> RedeemedCode | None:
        """Use a code once. None if unknown, used, expired or superseded."""
        ...

    async def cleanup(self, now: datetime, limits: ProvisioningLimits) -> AccountCleanup:
        """Delete requests that no longer count and codes past their day."""
        ...

    async def link(self, new: NewLink, now: datetime) -> LinkOutcome:
        """Redeem the code and link its person to `new.sub`, in one transaction.

        Only an unused, unexpired code whose consented email HMAC equals
        `new.email_hmac` links, and only a subject not linked to somebody
        else. A person already linked to another subject is relinked, and that
        subject's user sessions end. The code is used only when the link is made.
        """
        ...

    async def code_holder(self, code_sha256: bytes, now: datetime) -> DiscordProfile | None:
        """Whose live code this is, without using it. None if it would not link."""
        ...

    async def linked_person(self, sub: str) -> PersonRef | None:
        """Whom this CyberdyneAuth subject is linked to, or None."""
        ...

    async def linked_profile(self, sub: str) -> DiscordProfile | None:
        """`linked_person`, with the name the web shows for it."""
        ...

    async def unlink(self, person: PersonRef) -> bool:
        """Remove the person's link and end its user sessions. False if none."""
        ...

    async def unannounced_links(self, limit: int) -> Sequence[LinkNotice]:
        """Links the person has not been told about yet, oldest first."""
        ...

    async def mark_announced(self, notice: LinkNotice, now: datetime) -> None:
        """Record that the person was told, if the link is still that one."""
        ...
