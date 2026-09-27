"""`/account`: consent to exact values, per-person limits, HMAC email refs, link codes.

The service is driven over an in-memory store and a recording provisioner, so
what is asserted is what would leave the process and what would be stored.
The SQL that enforces the same limits under a lock is in
`tests/integration/test_accounts_store.py`.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import discord
import pytest
import structlog
from pydantic import ValidationError

from chatmemory.adapters.discord.accounts import consent_text, result_text, text
from chatmemory.adapters.discord.bot import CyberFriendClient
from chatmemory.app.accounts import (
    CONSENT_VERSION,
    AccountService,
    ConsentDraft,
    ProvisioningOutcome,
    ProvisioningResult,
    account_name,
    code_digest,
    email_hmac,
)
from chatmemory.app.language import Language
from chatmemory.composition import build_accounts
from chatmemory.config import Settings
from chatmemory.domain.identity import PersonRef, Viewer
from chatmemory.ports.accounts import (
    AccountCleanup,
    LinkCodeIssue,
    LinkCodeVerdict,
    ProvisioningInvalidName,
    ProvisioningLimits,
    ProvisioningRateLimited,
    ProvisioningRequest,
    ProvisioningUnavailable,
    RedeemedCode,
    Reservation,
    retry_at,
)
from chatmemory.ports.facts import FactKind, PersonalFact, PersonalFacts, StoredFact

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
KEY = b"k" * 32
LEO = PersonRef("discord", 42)
EMAIL = "leo@example.com"
URL = "https://console.example.com"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class MemoryStore:
    """`AccountStore` in memory, with the same limit rule as the SQL."""

    def __init__(self) -> None:
        self.requests: dict[int, tuple[PersonRef, bytes, datetime]] = {}
        self.consents: list[tuple[PersonRef, bytes, int]] = []
        self.codes: dict[bytes, tuple[PersonRef, datetime]] = {}
        self.seen: list[object] = []

    async def last_requests(self, person: PersonRef, since: datetime) -> Sequence[datetime]:
        return [at for who, _, at in self.requests.values() if who == person and at > since]

    async def reserve(
        self,
        person: PersonRef,
        email_hmac: bytes,
        consent_version: int,
        *,
        now: datetime,
        limits: ProvisioningLimits,
    ) -> Reservation:
        self.seen += [person, email_hmac, consent_version]
        wait = retry_at(await self.last_requests(person, now - limits.month), now, limits)
        if wait is not None:
            return Reservation(None, retry_at=wait)
        self.consents.append((person, email_hmac, consent_version))
        request_id = len(self.requests) + 1
        self.requests[request_id] = (person, email_hmac, now)
        return Reservation(request_id)

    async def release(self, request_id: int) -> None:
        del self.requests[request_id]

    async def issue_link_code(
        self,
        person: PersonRef,
        code_sha256: bytes,
        *,
        now: datetime,
        ttl: timedelta,
        per_day: int,
    ) -> LinkCodeIssue:
        self.seen.append(code_sha256)
        if not any(who == person for who, _, _ in self.consents):
            return LinkCodeIssue(LinkCodeVerdict.NO_CONSENT)
        self.codes[code_sha256] = (person, now + ttl)
        return LinkCodeIssue(LinkCodeVerdict.ISSUED)

    async def redeem_link_code(self, code_sha256: bytes, now: datetime) -> RedeemedCode | None:
        raise NotImplementedError

    async def cleanup(self, now: datetime, limits: ProvisioningLimits) -> AccountCleanup:
        raise NotImplementedError


class RecordingProvisioner:
    def __init__(self) -> None:
        self.sent: list[ProvisioningRequest] = []
        self.refusal: Exception | None = None

    async def request_account(self, request: ProvisioningRequest) -> None:
        if self.refusal is not None:
            raise self.refusal
        self.sent.append(request)


class Facts:
    """A fact store holding one person's facts; `facts_of` is all that is read."""

    def __init__(self, **values: str) -> None:
        self._facts = PersonalFacts(
            tuple(StoredFact(PersonalFact(FactKind(k), v), NOW) for k, v in values.items())
        )

    async def facts_of(self, viewer: Viewer) -> PersonalFacts:
        return self._facts if viewer.person == LEO else PersonalFacts()

    async def forget_all_facts(self, person: PersonRef) -> int:
        raise NotImplementedError

    async def set_fact(self, person: PersonRef, fact: PersonalFact) -> bool:
        raise NotImplementedError

    async def forget_fact(
        self, person: PersonRef, kind: FactKind, value: str | None = None
    ) -> bool:
        raise NotImplementedError


EVERYTHING = {
    "full_name": "Leonardo Araujo dos Santos",
    "preferred_name": "Leo",
    "email": EMAIL,
    "phone": "+5521980703795",
    "home_address": "Rio de Janeiro Brasil, Vargem Grande",
    "birth_date": "1981-06-21",
    "eth_wallet": "0xb26b933a075fbb3d4e8b0925cad4f2bc345475e0",
}


def service(
    facts: Facts | None = None,
    store: MemoryStore | None = None,
    provisioner: RecordingProvisioner | None = None,
    clock: Clock | None = None,
    code: str = "the-code",
) -> AccountService:
    return AccountService(
        store or MemoryStore(),
        provisioner or RecordingProvisioner(),
        facts or Facts(**EVERYTHING),
        email_key=KEY,
        link_base_url=URL + "/",
        clock=clock or Clock(),
        new_code=lambda: code,
    )


# --- what is sent ---------------------------------------------------------------


def test_the_request_has_no_field_for_anything_but_email_name_and_locale() -> None:
    names = [f.name for f in dataclasses.fields(ProvisioningRequest)]
    assert names == ["email", "name", "locale"]


async def test_only_email_name_and_locale_are_sent_whatever_else_is_on_file() -> None:
    provisioner = RecordingProvisioner()
    accounts = service(provisioner=provisioner)

    draft = await accounts.draft(LEO, "leo_discord", Language.PORTUGUESE)
    await accounts.confirm(LEO, draft)

    assert provisioner.sent == [
        ProvisioningRequest(email=EMAIL, name="Leonardo Araujo dos Santos", locale="pt-BR")
    ]
    sent = repr(provisioner.sent)
    for kind in ("phone", "home_address", "birth_date", "eth_wallet"):
        assert EVERYTHING[kind] not in sent


@pytest.mark.parametrize(
    ("facts", "display", "expected"),
    [
        ({"full_name": "Leonardo Santos", "preferred_name": "Leo"}, "Leo D", "Leonardo Santos"),
        ({"preferred_name": "Leo"}, "Leo D", "Leo"),
        ({}, "  Leo   D ", "Leo D"),
        ({}, "  ", None),
        # A name fact that looks like a domain is skipped for the next candidate.
        ({"preferred_name": "www.evil.com"}, "Leo D", "Leo D"),
        # A display name CyberdyneAuth would refuse sends no name at all.
        ({}, "leo_d", None),
    ],
)
async def test_the_name_is_the_full_name_else_preferred_else_display(
    facts: dict[str, str], display: str, expected: str | None
) -> None:
    draft = await service(Facts(**facts)).draft(LEO, display, Language.ENGLISH)
    assert draft.name == expected
    assert draft.email is None and draft.locale == "en"


@pytest.mark.parametrize(
    "display",
    [
        "<b>Leo</b>",
        "Leo > Ana",
        "https://evil.example/x",
        "www.evil.com",
        "evil.co",
        "Leo\x07",
        "Leo\u202eatad",
        "Leo\u200bD",
        "L" * 129,
    ],
)
async def test_a_display_name_with_links_markup_or_control_characters_is_not_sent(
    display: str,
) -> None:
    provisioner = RecordingProvisioner()
    accounts = service(Facts(email=EMAIL), provisioner=provisioner)

    draft = await accounts.draft(LEO, display, Language.ENGLISH)
    result = await accounts.confirm(LEO, draft)

    assert draft.name is None
    assert result.outcome is ProvisioningOutcome.REQUESTED
    assert provisioner.sent == [ProvisioningRequest(email=EMAIL, name=None, locale="en")]


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("J.R.R. Tolkien", "J.R.R. Tolkien"),
        ("José  da   Silva", "José da Silva"),
        ("D’Ávila-Neto", "D’Ávila-Neto"),
        ("www.evil.com", None),
        ("a/b", None),
    ],
)
def test_account_names_follow_the_full_name_rule_without_domains(
    candidate: str, expected: str | None
) -> None:
    assert account_name(candidate) == expected


def test_a_typed_email_is_checked_like_an_email_fact() -> None:
    draft = ConsentDraft(name="Leo", email=None, language=Language.ENGLISH)
    assert draft.with_email("  Leo@Example.com ") == dataclasses.replace(
        draft, email="Leo@example.com"
    )
    assert draft.with_email("not an email") is None
    assert draft.with_email("x@y.z`@evil") is None


# --- one reply for every outcome ------------------------------------------------------


async def test_the_reply_is_the_same_whether_the_account_was_new_or_existed() -> None:
    """The provider answers 202 either way, so both runs look the same."""
    replies = []
    for _existing in (False, True):
        accounts = service()
        result = await accounts.confirm(LEO, await accounts.draft(LEO, "", Language.ENGLISH))
        replies.append(result_text(result, Language.ENGLISH))
    assert replies[0] == replies[1] == text("requested", Language.ENGLISH)
    assert replies[0] == (
        "If this address doesn't have a CyberdyneAuth account yet, it will receive an "
        "invitation. Once you've accepted it, press **Link my account** (or use "
        "`/account link`) and I'll DM you a sign-in link."
    )


# --- limits ------------------------------------------------------------------------------


async def test_a_second_request_within_a_day_is_refused_and_nothing_is_sent() -> None:
    clock, provisioner = Clock(), RecordingProvisioner()
    accounts = service(provisioner=provisioner, clock=clock)
    draft = await accounts.draft(LEO, "", Language.ENGLISH)
    await accounts.confirm(LEO, draft)

    clock.now = NOW + timedelta(hours=23)
    second = await accounts.confirm(LEO, draft)

    assert second == ProvisioningResult(ProvisioningOutcome.LIMITED, NOW + timedelta(hours=24))
    assert len(provisioner.sent) == 1
    assert await accounts.next_allowed(LEO) == NOW + timedelta(hours=24)
    assert "You can ask again <t:" in result_text(second, Language.ENGLISH)


async def test_a_fourth_request_in_thirty_days_is_refused_and_nothing_is_sent() -> None:
    clock, provisioner = Clock(), RecordingProvisioner()
    accounts = service(provisioner=provisioner, clock=clock)
    draft = await accounts.draft(LEO, "", Language.ENGLISH)
    for day in (0, 2, 4):
        clock.now = NOW + timedelta(days=day)
        assert (await accounts.confirm(LEO, draft)).outcome is ProvisioningOutcome.REQUESTED

    clock.now = NOW + timedelta(days=6)
    fourth = await accounts.confirm(LEO, draft)

    assert fourth == ProvisioningResult(ProvisioningOutcome.LIMITED, NOW + timedelta(days=30))
    assert len(provisioner.sent) == 3
    clock.now = NOW + timedelta(days=30, seconds=1)
    assert (await accounts.confirm(LEO, draft)).outcome is ProvisioningOutcome.REQUESTED


@pytest.mark.parametrize(
    "refusal",
    [ProvisioningRateLimited(), ProvisioningInvalidName(), ProvisioningUnavailable()],
)
async def test_a_refused_request_says_try_later_and_is_not_counted(refusal: Exception) -> None:
    provisioner, store = RecordingProvisioner(), MemoryStore()
    accounts = service(provisioner=provisioner, store=store)
    draft = await accounts.draft(LEO, "", Language.ENGLISH)
    provisioner.refusal = refusal

    refused = await accounts.confirm(LEO, draft)

    assert refused.outcome is ProvisioningOutcome.TRY_LATER
    assert "try again later" in result_text(refused, Language.ENGLISH)
    assert store.requests == {}
    provisioner.refusal = None
    assert (await accounts.confirm(LEO, draft)).outcome is ProvisioningOutcome.REQUESTED


async def test_a_refused_name_warns_the_operator_without_the_name_or_email() -> None:
    """A 422 on a name `account_name` passed means the name rules drifted apart."""
    provisioner = RecordingProvisioner()
    accounts = service(provisioner=provisioner)
    draft = await accounts.draft(LEO, "", Language.ENGLISH)
    provisioner.refusal = ProvisioningInvalidName()

    with structlog.testing.capture_logs() as logs:
        await accounts.confirm(LEO, draft)

    rejected = [e for e in logs if e["event"] == "accounts.provisioning_name_rejected"]
    assert [e["log_level"] for e in rejected] == ["warning"]
    written = repr(logs)
    assert EMAIL not in written and EVERYTHING["full_name"] not in written


@pytest.mark.parametrize("refusal", [ProvisioningRateLimited(), ProvisioningUnavailable()])
async def test_other_refusals_do_not_warn_about_the_name(refusal: Exception) -> None:
    provisioner = RecordingProvisioner()
    accounts = service(provisioner=provisioner)
    draft = await accounts.draft(LEO, "", Language.ENGLISH)
    provisioner.refusal = refusal

    with structlog.testing.capture_logs() as logs:
        await accounts.confirm(LEO, draft)

    assert [e["log_level"] for e in logs if e["event"].startswith("accounts.")] == ["info"]


async def test_an_unexpected_failure_is_uncounted_too() -> None:
    provisioner, store = RecordingProvisioner(), MemoryStore()
    provisioner.refusal = RuntimeError("boom")
    accounts = service(provisioner=provisioner, store=store)

    result = await accounts.confirm(LEO, await accounts.draft(LEO, "", Language.ENGLISH))

    assert result.outcome is ProvisioningOutcome.TRY_LATER and store.requests == {}


def test_retry_at_needs_both_windows_clear() -> None:
    limits = ProvisioningLimits()
    assert retry_at([], NOW, limits) is None
    assert retry_at([NOW - timedelta(hours=25)], NOW, limits) is None
    assert retry_at([NOW - timedelta(hours=2)], NOW, limits) == NOW + timedelta(hours=22)
    three = [NOW - timedelta(days=d) for d in (29, 10, 5)]
    assert retry_at(three, NOW, limits) == NOW + timedelta(days=1)
    assert retry_at([*three, NOW - timedelta(days=31)], NOW, limits) == NOW + timedelta(days=1)


# --- the email is stored only as an HMAC -------------------------------------------


def test_email_hmac_is_keyed_and_ignores_case() -> None:
    digest = email_hmac(KEY, "Leo@Example.com ")
    assert digest == email_hmac(KEY, "leo@example.com")
    assert len(digest) == 32
    assert digest != email_hmac(b"j" * 32, "leo@example.com")
    assert digest != hashlib.sha256(b"leo@example.com").digest()


async def test_the_store_is_never_handed_the_email() -> None:
    store = MemoryStore()
    accounts = service(store=store)
    await accounts.confirm(LEO, await accounts.draft(LEO, "", Language.ENGLISH))
    await accounts.link_code(LEO)

    assert EMAIL not in repr(store.seen) and EMAIL.encode() not in repr(store.seen).encode()
    assert store.consents == [(LEO, email_hmac(KEY, EMAIL), CONSENT_VERSION)]


# --- link codes ------------------------------------------------------------------------


async def test_a_link_code_is_dmed_in_the_url_and_stored_hashed() -> None:
    store = MemoryStore()
    accounts = service(store=store, code="abc123")
    await accounts.confirm(LEO, await accounts.draft(LEO, "", Language.ENGLISH))

    issued = await accounts.link_code(LEO)

    assert issued.url == f"{URL}/link?code=abc123"
    assert list(store.codes) == [code_digest("abc123")]
    assert b"abc123" not in b"".join(store.codes)
    assert store.codes[code_digest("abc123")] == (LEO, NOW + timedelta(minutes=15))


async def test_no_link_code_without_consent() -> None:
    issued = await service().link_code(LEO)
    assert issued.verdict is LinkCodeVerdict.NO_CONSENT and issued.url is None


# --- the consent text ------------------------------------------------------------------

CONSENT_EN = (
    "**Create my CyberdyneAuth account**\n"
    "I'll send CyberdyneAuth exactly this, and nothing else about you:\n"
    "• Name: Leonardo Araujo dos Santos\n"
    "• Email: leo@example.com\n"
    "• Language: English\n\n"
    "CyberdyneAuth will email an invitation to that address. The account works only "
    "once the invitation is accepted, so nobody gets an account through an address "
    "they don't control.\n\n"
    "The CyberdyneAuth account, with this name and email, is **not** deleted by "
    "`/privacy` → Delete everything, because CyberdyneAuth can't delete accounts on "
    "request yet. To have it deleted, ask a server admin to request its deletion "
    "from the CyberdyneAuth team."
)

CONSENT_PT = (
    "**Criar minha conta CyberdyneAuth**\n"
    "Vou enviar à CyberdyneAuth exatamente isto, e nada mais sobre você:\n"
    "• Nome: Leonardo Araujo dos Santos\n"
    "• Email: leo@example.com\n"
    "• Idioma: português\n\n"
    "A CyberdyneAuth vai mandar um convite para esse endereço. A conta só funciona "
    "depois que o convite for aceito, então ninguém ganha uma conta com um endereço "
    "que não controla.\n\n"
    "A conta CyberdyneAuth, com este nome e email, **não** é apagada pelo "
    "`/privacy` → Apagar tudo, porque a CyberdyneAuth ainda não apaga contas a "
    "pedido. Para apagá-la, peça a um admin do servidor que solicite a exclusão à "
    "equipe da CyberdyneAuth."
)


@pytest.mark.parametrize(
    ("language", "expected"), [(Language.ENGLISH, CONSENT_EN), (Language.PORTUGUESE, CONSENT_PT)]
)
def test_the_consent_text(language: Language, expected: str) -> None:
    draft = ConsentDraft(name="Leonardo Araujo dos Santos", email=EMAIL, language=language)
    assert consent_text(draft) == expected


def test_the_consent_escapes_markdown_in_a_display_name() -> None:
    draft = ConsentDraft(name="**boss**", email=EMAIL, language=Language.ENGLISH)
    assert "• Name: \\*\\*boss\\*\\*" in consent_text(draft)


# --- configuration and wiring ----------------------------------------------------------

BASE = {
    "discord_token": "t",
    "discord_guild_id": 1,
    "database_url": "postgresql+asyncpg://x/y",
    "llm_api_key": "k",
}


def test_provisioning_is_off_by_default() -> None:
    settings = Settings.model_validate(BASE)
    assert settings.account_provisioning_enabled is False
    assert build_accounts(settings, object(), RecordingProvisioner()) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"provisioning_email_key": "k" * 32},
        {"admin_public_url": URL},
        {"provisioning_email_key": "short", "admin_public_url": URL},
    ],
)
def test_enabling_needs_a_long_key_and_the_public_url(extra: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**BASE, "account_provisioning_enabled": True, **extra})


@pytest.mark.parametrize(
    "url",
    [
        "http://admin.example.com",
        "https://admin.example.com/?a=b",
        "https://admin.example.com/#top",
        "https://",
        "admin.example.com",
    ],
)
def test_the_public_url_is_https_without_query_or_fragment(url: str) -> None:
    """The link code is a bearer secret appended as `{url}/link?code=...`."""
    with pytest.raises(ValidationError):
        Settings.model_validate({**BASE, "admin_public_url": url})


def test_the_public_url_loses_its_trailing_slash() -> None:
    settings = Settings.model_validate({**BASE, "admin_public_url": "https://admin.example.com/"})
    assert settings.admin_public_url == "https://admin.example.com"


def test_enabled_without_a_provisioner_stays_hidden() -> None:
    settings = Settings.model_validate(
        {
            **BASE,
            "account_provisioning_enabled": True,
            "provisioning_email_key": "k" * 32,
            "admin_public_url": URL,
        }
    )
    assert build_accounts(settings, object(), None) is None  # type: ignore[arg-type]
    assert build_accounts(settings, object(), RecordingProvisioner()) is not None  # type: ignore[arg-type]


async def _registered(accounts: AccountService | None, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    client = CyberFriendClient(object(), 1)  # type: ignore[arg-type]

    async def sync(*, guild: object = None) -> list[object]:
        return []

    monkeypatch.setattr(client.tree, "sync", sync)
    if accounts is not None:
        client.attach_accounts(accounts)
    await client.setup_hook()
    commands = {c.name: c for c in client.tree.get_commands()}
    if "account" in commands:
        contexts = commands["account"].allowed_contexts
        assert contexts is not None and contexts.dm_channel and contexts.guild
    assert not {c.name for c in client.tree.get_commands(guild=discord.Object(1))} & {"account"}
    return set(commands)


async def test_the_command_is_offered_only_when_attached(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "account" not in await _registered(None, monkeypatch)
    assert "account" in await _registered(service(), monkeypatch)
