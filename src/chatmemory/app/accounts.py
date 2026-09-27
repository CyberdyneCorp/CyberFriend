"""Asking for a CyberdyneAuth account, with consent to exact values.

`AccountService` is the policy a person meets at `/account`:

*   **Exact values, then consent.** `draft` shows what would be sent: the
    email fact (or one the person types, which is not saved as a fact), the
    full name, else the preferred name, else the display name, and the answer
    language. `confirm` sends exactly that through `ProvisioningRequest`,
    which has no field for anything else.
*   **Limited per person.** At most 1 request in 24 hours and 3 in 30 days,
    counted in the store under a lock before the provider is called. Over the
    limit nothing is sent and the person is told when to come back. A request
    the provider refuses (429 or any failure) is uncounted.
*   **One reply for every accepted outcome.** The provider answers 202 whether
    the account was created, already existed or was throttled, so
    `ProvisioningOutcome.REQUESTED` is all there is to say.
*   **The email is never stored.** Only `email_hmac`, keyed with
    `PROVISIONING_EMAIL_KEY`, reaches the store; a plain hash of an address
    can be reversed by trying likely addresses, an HMAC cannot without the key.
*   **Link codes are proof, not identity.** `link_code` issues a fresh
    single-use code valid for 15 minutes, ends the earlier ones, and at most 5
    a day. Only its sha256 is stored; the code itself is in the DM'd URL.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

import structlog

from chatmemory.app.clock import Clock, utc_now
from chatmemory.app.language import Language
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.accounts import (
    AccountCleanup,
    AccountProvisioner,
    AccountStore,
    LinkCodeVerdict,
    ProvisioningFailed,
    ProvisioningLimits,
    ProvisioningRequest,
    retry_at,
)
from chatmemory.ports.facts import FactKind, FactStore, InvalidFact, normalise_fact

log = structlog.get_logger()

CONSENT_VERSION = 1
"""Bumped whenever the consent text changes what it tells people."""

LINK_CODE_TTL = timedelta(minutes=15)
LINK_CODES_PER_DAY = 5
LINK_CODE_BYTES = 32
LINK_PATH = "/link"

LOCALES = {Language.PORTUGUESE: "pt-BR"}
"""The locale sent with the request; English for everything else."""
DEFAULT_LOCALE = "en"

LIMITS = ProvisioningLimits()
"""1 request per person in 24 hours, 3 in 30 days."""


def email_hmac(key: bytes, email: str) -> bytes:
    """HMAC-SHA256 of the lowercased address: the only form an email is stored in."""
    return hmac.new(key, email.strip().lower().encode(), hashlib.sha256).digest()


def code_digest(code: str) -> bytes:
    """What is stored of a link code: its sha256."""
    return hashlib.sha256(code.encode()).digest()


def locale_for(language: Language) -> str:
    return LOCALES.get(language, DEFAULT_LOCALE)


@dataclass(frozen=True, slots=True)
class ConsentDraft:
    """What would be sent, shown to the person before they confirm."""

    name: str | None
    email: str | None
    language: Language

    @property
    def locale(self) -> str:
        return locale_for(self.language)

    def request(self) -> ProvisioningRequest:
        if self.email is None:
            raise ValueError("no email to request an account for")
        return ProvisioningRequest(email=self.email, name=self.name, locale=self.locale)

    def with_email(self, typed: str) -> ConsentDraft | None:
        """The draft with an address the person typed, or None if it is not one."""
        try:
            return replace(self, email=normalise_fact(FactKind.EMAIL, typed))
        except InvalidFact:
            return None


class ProvisioningOutcome(StrEnum):
    REQUESTED = "requested"
    """Accepted by the provider, whatever it did with it."""
    LIMITED = "limited"
    """Over the person's own limit; nothing was sent."""
    TRY_LATER = "try_later"
    """The provider did not accept it; nothing was counted."""


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    outcome: ProvisioningOutcome
    retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LinkCode:
    verdict: LinkCodeVerdict
    url: str | None = None
    retry_at: datetime | None = None


class AccountService:
    """Consent, provisioning and link codes for one deployment."""

    def __init__(
        self,
        store: AccountStore,
        provisioner: AccountProvisioner,
        facts: FactStore,
        *,
        email_key: bytes,
        link_base_url: str,
        limits: ProvisioningLimits = LIMITS,
        clock: Clock = utc_now,
        new_code: Callable[[], str] = lambda: secrets.token_urlsafe(LINK_CODE_BYTES),
    ) -> None:
        self._store = store
        self._provisioner = provisioner
        self._facts = facts
        self._key = email_key
        self._link_url = link_base_url.rstrip("/") + LINK_PATH
        self._limits = limits
        self._clock = clock
        self._new_code = new_code

    async def next_allowed(self, person: PersonRef) -> datetime | None:
        """When they may ask again, or None if they may now. Advisory: the
        limit that holds is the one `confirm` checks under a lock."""
        now = self._clock()
        earlier = await self._store.last_requests(person, now - self._limits.month)
        return retry_at(earlier, now, self._limits)

    async def draft(self, person: PersonRef, display_name: str, language: Language) -> ConsentDraft:
        facts = await self._facts.facts_of(Viewer(person, frozenset()))
        name = (
            facts.get(FactKind.FULL_NAME)
            or facts.get(FactKind.PREFERRED_NAME)
            or display_name.strip()
            or None
        )
        return ConsentDraft(name=name, email=facts.get(FactKind.EMAIL), language=language)

    async def confirm(self, person: PersonRef, draft: ConsentDraft) -> ProvisioningResult:
        request = draft.request()
        reservation = await self._store.reserve(
            person,
            email_hmac(self._key, request.email),
            CONSENT_VERSION,
            now=self._clock(),
            limits=self._limits,
        )
        if reservation.request_id is None:
            return ProvisioningResult(ProvisioningOutcome.LIMITED, reservation.retry_at)
        try:
            await self._provisioner.request_account(request)
        except Exception as exc:
            # Uncounted whatever went wrong: the person did not get a request.
            await self._store.release(reservation.request_id)
            if not isinstance(exc, ProvisioningFailed):
                log.exception("accounts.provisioning_failed")
            else:
                log.info("accounts.provisioning_refused", reason=type(exc).__name__)
            return ProvisioningResult(ProvisioningOutcome.TRY_LATER)
        log.info("accounts.provisioning_requested")
        return ProvisioningResult(ProvisioningOutcome.REQUESTED)

    async def link_code(self, person: PersonRef) -> LinkCode:
        code = self._new_code()
        issued = await self._store.issue_link_code(
            person,
            code_digest(code),
            now=self._clock(),
            ttl=LINK_CODE_TTL,
            per_day=LINK_CODES_PER_DAY,
        )
        if issued.verdict is not LinkCodeVerdict.ISSUED:
            return LinkCode(issued.verdict, retry_at=issued.retry_at)
        return LinkCode(LinkCodeVerdict.ISSUED, url=f"{self._link_url}?code={code}")


class AccountRecordsRetention:
    """Deletes requests past the 30 days they count for, and old link codes."""

    def __init__(self, store: AccountStore, limits: ProvisioningLimits = LIMITS) -> None:
        self._store = store
        self._limits = limits

    async def sweep(self, now: datetime) -> AccountCleanup:
        cleaned = await self._store.cleanup(now, self._limits)
        if cleaned.requests or cleaned.codes:
            log.info("accounts.cleanup", requests=cleaned.requests, codes=cleaned.codes)
        return cleaned
