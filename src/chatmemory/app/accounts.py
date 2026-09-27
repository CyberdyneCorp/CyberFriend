"""Asking for a CyberdyneAuth account, with consent to exact values.

`AccountService` is the policy a person meets at `/account`:

*   **Exact values, then consent.** `draft` shows what would be sent: the
    email fact (or one the person types, which is not saved as a fact), the
    full name, else the preferred name, else the display name (the first that
    passes `account_name`, so no link, markup or control character; none sends
    no name), and the answer language. `confirm` sends exactly that through `ProvisioningRequest`,
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
*   **A link is made by proof.** `AccountLinking` (the admin process, after a
    verified CyberdyneAuth sign-in in the browser that followed the link)
    links only when the signed-in email, verified, has the consented HMAC.
    `LinkAnnouncements` (the bot) then DMs "Linked to a***@domain, not you?
    [Unlink]", and `AccountService.unlink` undoes it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Awaitable, Callable
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
    DiscordProfile,
    LinkCodeVerdict,
    LinkNotice,
    LinkOutcome,
    NewLink,
    ProvisioningFailed,
    ProvisioningLimits,
    ProvisioningRequest,
    retry_at,
)
from chatmemory.ports.facts import FactKind, FactStore, InvalidFact, normalise_fact
from chatmemory.ports.privacy import ERASED_NAME

log = structlog.get_logger()

CONSENT_VERSION = 1
"""Bumped whenever the consent text changes what it tells people."""

LINK_CODE_TTL = timedelta(minutes=15)
LINK_CODES_PER_DAY = 5
LINK_CODE_BYTES = 32
LINK_PATH = "/link"
USER_AREA_PATH = "/#/me"

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


# A dot between two letters/digits followed by two or more letters: `www.evil.com`,
# `evil.co`. Initials such as "J.R.R. Tolkien" have one letter after each dot.
_DOMAIN_LIKE = re.compile(r"[^\W_]\.[^\W\d_]{2,}")


def account_name(candidate: str) -> str | None:
    """`candidate` as a name the identity provider will take, or None.

    It must pass the full-name fact check (letters, marks, digits, spaces and
    `'-.’`, at most 128 characters: no `<`, `>`, `/`, `:`, control or bidi
    characters) and must not look like a domain, so no link can be sent or
    shown as a name.
    """
    try:
        name = normalise_fact(FactKind.FULL_NAME, candidate)
    except InvalidFact:
        return None
    return None if _DOMAIN_LIKE.search(name) else name


def mask_email(email: str) -> str:
    """`a***@example.com`: enough for the owner to recognise, not to read off."""
    local, _, domain = email.strip().rpartition("@")
    return f"{local[:1]}***@{domain}" if local else "***"


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
        self._user_area_url = link_base_url.rstrip("/") + USER_AREA_PATH
        self._limits = limits
        self._clock = clock
        self._new_code = new_code

    @property
    def user_area_url(self) -> str:
        """Where a linked person sees their data on the web."""
        return self._user_area_url

    async def next_allowed(self, person: PersonRef) -> datetime | None:
        """When they may ask again, or None if they may now. Advisory: the
        limit that holds is the one `confirm` checks under a lock."""
        now = self._clock()
        earlier = await self._store.last_requests(person, now - self._limits.month)
        return retry_at(earlier, now, self._limits)

    async def draft(self, person: PersonRef, display_name: str, language: Language) -> ConsentDraft:
        facts = await self._facts.facts_of(Viewer(person, frozenset()))
        candidates = (
            facts.get(FactKind.FULL_NAME),
            facts.get(FactKind.PREFERRED_NAME),
            display_name,
        )
        names = (account_name(c) for c in candidates if c)
        name = next((n for n in names if n is not None), None)
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

    async def unlink(self, person: PersonRef) -> bool:
        """[Unlink]: the person's web account stops reaching their data at once."""
        unlinked = await self._store.unlink(person)
        log.info("accounts.unlinked", done=unlinked)
        return unlinked


class AccountLinking:
    """Links a signed-in CyberdyneAuth subject to the person whose code it was.

    For the admin process. The caller has verified the sign-in (state, the
    browser binding, the id token and its nonce, `userinfo.sub` equal to the
    token subjects) and that the email is verified; this checks the rest
    against the code, in one transaction in the store.
    """

    def __init__(
        self, store: AccountStore, *, email_key: bytes, clock: Clock = utc_now
    ) -> None:
        self._store = store
        self._key = email_key
        self._clock = clock

    async def link(self, code_sha256: bytes, *, email: str, issuer: str, sub: str) -> LinkOutcome:
        outcome = await self._store.link(
            NewLink(
                code_sha256=code_sha256,
                email_hmac=email_hmac(self._key, email),
                issuer=issuer,
                sub=sub,
                email_hint=mask_email(email),
            ),
            self._clock(),
        )
        log.info("accounts.link", outcome=outcome.value)
        return outcome

    async def holder(self, code_sha256: bytes) -> DiscordProfile | None:
        """Whose live code this is, for `/link` to name before signing in."""
        return await self._store.code_holder(code_sha256, self._clock())

    async def person_for(self, sub: str) -> PersonRef | None:
        """The person a signed-in subject is linked to; None if it is not."""
        return await self._store.linked_person(sub)

    async def profile_for(self, sub: str) -> DiscordProfile | None:
        """`person_for`, with the name `/me` shows, so a wrong link is visible."""
        return await self._store.linked_profile(sub)

    async def unlink(self, person: PersonRef) -> bool:
        """Unlink from the web: the same as [Unlink] in the DM."""
        unlinked = await self._store.unlink(person)
        log.info("accounts.unlinked", done=unlinked, via="web")
        return unlinked


def discord_label(profile: DiscordProfile) -> str:
    """"Leo (Discord user 7)", or "Discord user 7" while the name is a placeholder.

    A person row's name starts as the platform id and is replaced by a real
    one when one is known; the id is always shown, since a name can be copied.
    """
    user = f"Discord user {profile.person.platform_user_id}"
    name = profile.display_name.strip()
    if not name or name == str(profile.person.platform_user_id) or name == ERASED_NAME:
        return user
    return f"{name} ({user})"


LINK_NOTICE_BATCH = 20

Announce = Callable[[LinkNotice], Awaitable[bool]]
"""Tell the person about a link. True when there is nothing more to do (sent,
or the DM is closed for good); False to try again on the next pass."""


class LinkAnnouncements:
    """The bot's half of a link: "Linked to a***@domain, not you? [Unlink]"."""

    def __init__(self, store: AccountStore, clock: Clock = utc_now) -> None:
        self._store = store
        self._clock = clock

    async def announce(self, tell: Announce) -> int:
        """Tell everyone linked since the last pass. Returns how many were done."""
        done = 0
        for notice in await self._store.unannounced_links(LINK_NOTICE_BATCH):
            if await tell(notice):
                await self._store.mark_announced(notice, self._clock())
                done += 1
        return done


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
