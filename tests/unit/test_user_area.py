"""The web user area through the real console app, over FakeOIDC.

`/link` links a CyberdyneAuth account to the person whose DM'd code it is,
only in the browser that opened it, only for the verified, consented email,
and only when the subjects agree. `/me` is reached with the user cookie alone:
the console's cookie and bearer tokens are 401 there, and the user cookie is
401 on every console route. "Delete everything" needs a sign-in from the last
five minutes, proven by the id token's `auth_time`.
"""

from __future__ import annotations

import hmac
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from chatmemory.admin.auth import SESSION_COOKIE
from chatmemory.admin.server import build_app
from chatmemory.admin.user.auth import USER_COOKIE
from chatmemory.admin.user.routes import ACCOUNT_NOT_DELETED, NOT_LINKED, UserArea
from chatmemory.admin.user.service import UserSignIn
from chatmemory.admin.user.store import InMemoryUserSessionStore
from chatmemory.app.accounts import AccountLinking, code_digest, email_hmac, mask_email
from chatmemory.app.feature_requests import FeatureRequestService
from chatmemory.app.privacy import PrivacyService, RetentionFacts
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.accounts import LinkOutcome, NewLink
from chatmemory.ports.facts import FactKind
from chatmemory.ports.feature_requests import (
    FeatureRequest,
    NewSuggestion,
    StoreResult,
    StoreVerdict,
)
from chatmemory.ports.privacy import (
    ErasureCounts,
    ErasureMode,
    ErasureRequest,
    ErasureStep,
    HeldFact,
    Inventory,
)
from tests.unit.oidc_support import CSRF, PUBLIC_URL, Rig, rig
from tests.unit.test_admin_api import build_console

KEY = b"k" * 32
LEO = PersonRef("discord", 7)
ANA = PersonRef("discord", 8)
LEO_EMAIL = "leo@cyberdyne.test"
CODE = "leo-link-code"


@dataclass
class Code:
    person: PersonRef
    email_hmac: bytes
    expires_at: datetime
    used: bool = False


@dataclass
class MemoryAccounts:
    """The linking half of `AccountStore`, with the same rules as the SQL."""

    codes: dict[bytes, Code] = field(default_factory=dict)
    links: dict[str, PersonRef] = field(default_factory=dict)

    def issue(self, person: PersonRef, code: str, email: str, expires_at: datetime) -> None:
        self.codes[code_digest(code)] = Code(person, email_hmac(KEY, email), expires_at)

    async def link(self, new: NewLink, now: datetime) -> LinkOutcome:
        code = self.codes.get(new.code_sha256)
        if code is None or code.used or code.expires_at <= now:
            return LinkOutcome.BAD_CODE
        if not hmac.compare_digest(code.email_hmac, new.email_hmac):
            return LinkOutcome.OTHER_EMAIL
        owner = self.links.get(new.sub)
        if owner is not None and owner != code.person:
            return LinkOutcome.SUBJECT_TAKEN
        self.links = {s: p for s, p in self.links.items() if p != code.person}
        self.links[new.sub] = code.person
        code.used = True
        return LinkOutcome.LINKED

    async def linked_person(self, sub: str) -> PersonRef | None:
        return self.links.get(sub)


class Inventories:
    """`PrivacyStore`: each person's own facts."""

    async def inventory(
        self, person: PersonRef, readable_channel_ids: Sequence[int], month: date
    ) -> Inventory:
        email = LEO_EMAIL if person == LEO else "ana@cyberdyne.test"
        return Inventory(
            known=True, platforms=("discord",), facts=(HeldFact(FactKind.EMAIL, email),)
        )


@dataclass
class Erasures:
    """The erasure, with its purge of the link and the user sessions."""

    accounts: MemoryAccounts
    sessions: InMemoryUserSessionStore
    done: list[tuple[PersonRef, ErasureMode]] = field(default_factory=list)

    async def erase(
        self, person: PersonRef, mode: ErasureMode, counts: ErasureCounts
    ) -> ErasureRequest:
        self.done.append((person, mode))
        for sub in [s for s, p in self.accounts.links.items() if p == person]:
            del self.accounts.links[sub]
            self.sessions.end_subject(sub)
        return ErasureRequest(
            1, 1, person, mode, ErasureStep.COMPLETE, datetime.now(UTC), counts
        )


class Suggestions:
    def __init__(self) -> None:
        self.submitted: list[NewSuggestion] = []

    async def submit(
        self, suggestion: NewSuggestion, *, now: datetime, daily_limit: int
    ) -> StoreResult:
        self.submitted.append(suggestion)
        return StoreResult(StoreVerdict.STORED, len(self.submitted))

    async def for_person(self, person: PersonRef, limit: int) -> Sequence[FeatureRequest]:
        return []

    async def set_notify(self, person: PersonRef, request_id: int, notify: bool) -> bool:
        return True


@dataclass
class Area:
    signing: Rig
    client: TestClient
    accounts: MemoryAccounts
    sessions: InMemoryUserSessionStore
    erasures: Erasures
    suggestions: Suggestions
    admin_token: str

    def browser(self) -> TestClient:
        return TestClient(self.client.app, base_url=PUBLIC_URL)

    def follow_link(self, browser: TestClient, sub: str, code: str = CODE) -> int:
        started = browser.get("/link", params={"code": code}, follow_redirects=False)
        assert started.status_code == 302, started.text
        oidc_code, state = self.signing.fake.authorize(started.headers["location"], sub)
        done = browser.get(
            "/auth/callback", params={"code": oidc_code, "state": state}, follow_redirects=False
        )
        return done.status_code

    def sign_in(self, browser: TestClient, sub: str, route: str = "/auth/user/login") -> int:
        started = browser.get(route, follow_redirects=False)
        assert started.status_code == 302, started.text
        oidc_code, state = self.signing.fake.authorize(started.headers["location"], sub)
        done = browser.get(
            "/auth/callback", params={"code": oidc_code, "state": state}, follow_redirects=False
        )
        return done.status_code


@pytest.fixture
async def area() -> Area:
    signing = rig()
    fake = signing.fake
    fake.add("leo-sub", LEO_EMAIL, [])
    fake.add("mallory-sub", "mallory@cyberdyne.test", None)
    fake.add("unverified-sub", LEO_EMAIL, None, email_verified=False)
    accounts = MemoryAccounts()
    accounts.issue(LEO, CODE, LEO_EMAIL, signing.clock.now + timedelta(minutes=15))
    sessions = InMemoryUserSessionStore()
    erasures = Erasures(accounts, sessions)
    suggestions = Suggestions()
    user_area = UserArea(
        sign_in=UserSignIn(
            signing.sign_in,
            sessions,
            AccountLinking(accounts, email_key=KEY, clock=signing.clock),  # type: ignore[arg-type]
            clock=signing.clock,
        ),
        privacy=PrivacyService(
            Inventories(),
            RetentionFacts(
                tracing=False,
                trace_retention_days=90,
                memory_retention_days=30,
                backup_retention_days=None,
            ),
            erasure=erasures,  # type: ignore[arg-type]
        ),
        suggestions=FeatureRequestService(suggestions, clock=signing.clock),
    )
    console = await build_console(sign_in=signing.sign_in)
    app = build_app(
        console.services, console.tokens, sign_in=signing.sign_in, user_area=user_area
    )
    return Area(
        signing,
        TestClient(app, base_url=PUBLIC_URL),
        accounts,
        sessions,
        erasures,
        suggestions,
        console.credential,
    )


# --- linking ------------------------------------------------------------------


def test_a_link_code_followed_and_signed_in_links_and_opens_the_user_area(area: Area) -> None:
    status = area.follow_link(area.client, "leo-sub")

    assert status == 302
    assert area.accounts.links == {"leo-sub": LEO}
    assert area.client.cookies.get(USER_COOKIE)
    assert SESSION_COOKIE not in area.client.cookies
    assert area.signing.fake.authorizations[-1]["prompt"] == "login"
    session = area.client.get("/me/session").json()
    assert session == {"email": LEO_EMAIL, "linked": True, "fresh": False}


def test_an_unverified_email_is_refused(area: Area) -> None:
    status = area.follow_link(area.client, "unverified-sub")

    assert status == 403
    assert area.accounts.links == {}
    assert USER_COOKIE not in area.client.cookies
    assert not area.accounts.codes[code_digest(CODE)].used, "the owner can still use it"


def test_a_different_email_is_refused(area: Area) -> None:
    """A forwarded code, signed in as somebody else: no link."""
    assert area.follow_link(area.client, "mallory-sub") == 403
    assert area.accounts.links == {}
    assert USER_COOKIE not in area.client.cookies


def test_a_subject_mismatch_is_refused(area: Area) -> None:
    area.signing.fake.userinfo_sub = "someone-else"

    assert area.follow_link(area.client, "leo-sub") == 400
    assert area.accounts.links == {}


def test_a_callback_in_another_browser_is_refused(area: Area) -> None:
    started = area.client.get("/link", params={"code": CODE}, follow_redirects=False)
    oidc_code, state = area.signing.fake.authorize(started.headers["location"], "leo-sub")

    victim = area.browser()
    response = victim.get(
        "/auth/callback", params={"code": oidc_code, "state": state}, follow_redirects=False
    )

    assert response.status_code == 400
    assert area.accounts.links == {}
    assert USER_COOKIE not in victim.cookies


def test_a_used_code_is_refused(area: Area) -> None:
    assert area.follow_link(area.client, "leo-sub") == 302
    area.accounts.links.clear()

    assert area.follow_link(area.browser(), "leo-sub") == 403
    assert area.accounts.links == {}


def test_an_expired_code_is_refused(area: Area) -> None:
    area.signing.clock.advance(timedelta(minutes=16))

    assert area.follow_link(area.client, "leo-sub") == 403
    assert area.accounts.links == {}


def test_a_link_without_a_code_starts_nothing(area: Area) -> None:
    response = area.client.get("/link", follow_redirects=False)
    assert response.status_code == 400
    assert area.signing.logins.records == {}


def test_the_masked_email_names_only_the_first_letter_and_the_domain() -> None:
    assert mask_email("leonardo@example.com") == "l***@example.com"


# --- the two sessions never cross ---------------------------------------------------


def test_each_cookie_is_401_on_the_others_routes(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    user_cookie = area.client.cookies[USER_COOKIE]
    admin = area.browser()
    area.signing.fake.add("ana-admin", "ana@cyberdyne.test", [area.signing.fake.role("admin")])
    assert area.sign_in(admin, "ana-admin", "/auth/login") == 302
    admin_cookie = admin.cookies[SESSION_COOKIE]

    user_on_console = area.browser().get(
        "/api/status", headers={"Cookie": f"{USER_COOKIE}={user_cookie}"}
    )
    admin_on_me = area.browser().get(
        "/me/privacy", headers={"Cookie": f"{SESSION_COOKIE}={admin_cookie}"}
    )
    bearer_on_me = area.browser().get(
        "/me/privacy", headers={"Authorization": f"Bearer {area.admin_token}"}
    )
    both_on_me = area.browser().get(
        "/me/privacy",
        headers={
            "Authorization": f"Bearer {area.admin_token}",
            "Cookie": f"{USER_COOKIE}={user_cookie}",
        },
    )

    assert user_on_console.status_code == 401
    assert admin_on_me.status_code == 401
    assert bearer_on_me.status_code == 401
    assert both_on_me.status_code == 401
    assert area.client.get("/me/privacy").status_code == 200


def test_a_user_session_does_not_open_unclaimed_paths_under_me(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    assert area.client.get("/me/anything").status_code == 403
    assert area.browser().get("/me/anything").status_code == 401


def test_without_the_user_area_its_routes_are_not_there() -> None:
    signing = rig()

    async def app() -> TestClient:
        console = await build_console(sign_in=signing.sign_in)
        return TestClient(
            build_app(console.services, console.tokens, sign_in=signing.sign_in),
            base_url=PUBLIC_URL,
        )

    import asyncio

    client = asyncio.run(app())
    assert client.get("/link", params={"code": "x"}, follow_redirects=False).status_code == 404
    assert client.get("/me/privacy").status_code == 401


# --- whose data -------------------------------------------------------------------


def test_the_privacy_dashboard_is_the_linked_persons_whatever_is_asked(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")

    mine = area.client.get(
        "/me/privacy", params={"person_id": "8", "platform_user_id": str(ANA.platform_user_id)}
    )

    assert mine.status_code == 200
    body = mine.json()
    assert body["facts"] == [{"kind": "email", "value": LEO_EMAIL}]
    assert body["retention"]["backup_retention_days"] is None
    assert body["identity_account"] == ACCOUNT_NOT_DELETED


def test_an_unlinked_account_and_an_unknown_one_get_the_same_answer(area: Area) -> None:
    area.signing.fake.add("stranger-sub", "stranger@cyberdyne.test", None)
    assert area.sign_in(area.client, "mallory-sub") == 302
    unlinked = area.client.get("/me/privacy")
    other = area.browser()
    assert area.sign_in(other, "stranger-sub") == 302
    unknown = other.get("/me/privacy")

    assert unlinked.status_code == unknown.status_code == 404
    assert unlinked.json() == unknown.json() == {"error": NOT_LINKED}
    assert other.get("/me/session").json()["linked"] is False


def test_a_suggestion_with_a_phone_number_is_refused_with_the_reason(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")

    refused = area.client.post(
        "/me/feature-requests", json={"text": "call me on +55 11 98765 4321"}, headers=CSRF
    )
    stored = area.client.post(
        "/me/feature-requests", json={"text": "a dark mode please"}, headers=CSRF
    )

    assert refused.status_code == 422
    assert "phone number" in refused.json()["message"]
    assert stored.status_code == 201
    [suggestion] = area.suggestions.submitted
    assert suggestion.person == LEO and suggestion.source.kind.value == "web"


def test_a_suggestion_without_the_console_header_is_refused(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    response = area.client.post("/me/feature-requests", json={"text": "dark mode"})
    assert response.status_code == 403
    assert area.suggestions.submitted == []


# --- delete everything -------------------------------------------------------------

ERASE = {"mode": "erase_and_opt_out", "confirm": "DELETE"}


def test_erasing_needs_a_fresh_sign_in(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")

    stale = area.client.post("/me/erase", json=ERASE, headers=CSRF)

    assert stale.status_code == 403
    assert stale.json()["reauth"] == "/auth/user/fresh"
    assert area.erasures.done == []


def test_a_fresh_sign_in_asks_for_max_age_and_records_auth_time(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")

    assert area.sign_in(area.client, "leo-sub", "/auth/user/fresh") == 302

    assert area.signing.fake.authorizations[-1]["max_age"] == "300"
    assert area.client.get("/me/session").json()["fresh"] is True


def test_a_stale_fresh_sign_in_is_refused(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    area.sign_in(area.client, "leo-sub", "/auth/user/fresh")
    area.signing.clock.advance(timedelta(minutes=6))

    response = area.client.post("/me/erase", json=ERASE, headers=CSRF)

    assert response.status_code == 403
    assert area.erasures.done == []


def test_an_id_token_without_auth_time_after_max_age_is_refused(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    before = dict(area.sessions.records)
    area.signing.fake.id_overrides = {"auth_time": None}

    assert area.sign_in(area.client, "leo-sub", "/auth/user/fresh") == 400
    assert area.sessions.records == before


def test_an_old_auth_time_after_max_age_is_refused(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    old = int((area.signing.clock.now - timedelta(minutes=10)).timestamp())
    area.signing.fake.id_overrides = {"auth_time": old}

    assert area.sign_in(area.client, "leo-sub", "/auth/user/fresh") == 400


def test_erasing_needs_the_typed_word_and_the_console_header(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    area.sign_in(area.client, "leo-sub", "/auth/user/fresh")

    unconfirmed = area.client.post(
        "/me/erase", json={**ERASE, "confirm": "delete"}, headers=CSRF
    )
    cross_site = area.client.post("/me/erase", json=ERASE)
    other_origin = area.client.post(
        "/me/erase", json=ERASE, headers={**CSRF, "Origin": "https://evil.test"}
    )

    assert unconfirmed.status_code == 400
    assert cross_site.status_code == other_origin.status_code == 403
    assert area.erasures.done == []


def test_erasing_deletes_with_the_chosen_mode_and_ends_the_session(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    area.sign_in(area.client, "leo-sub", "/auth/user/fresh")
    cookie = area.client.cookies[USER_COOKIE]

    erased = area.client.post("/me/erase", json=ERASE, headers=CSRF)

    assert erased.status_code == 200, erased.text
    assert erased.json()["identity_account"] == ACCOUNT_NOT_DELETED
    assert area.erasures.done == [(LEO, ErasureMode.ERASE_AND_OPT_OUT)]
    assert area.accounts.links == {}
    assert area.signing.fake.revoked, "the refresh token was not revoked at the issuer"
    cleared = [h for h in erased.headers.get_list("set-cookie") if h.startswith(USER_COOKIE)]
    assert cleared and "Max-Age=0" in cleared[0]
    replay = area.browser().get("/me/session", headers={"Cookie": f"{USER_COOKIE}={cookie}"})
    assert replay.status_code == 401


def test_signing_out_ends_the_user_session(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    cookie = area.client.cookies[USER_COOKIE]

    out = area.client.post("/me/logout", headers=CSRF)

    assert out.status_code == 200
    replay = area.browser().get("/me/session", headers={"Cookie": f"{USER_COOKIE}={cookie}"})
    assert replay.status_code == 401
    assert all(r.revoked_at is not None for r in area.sessions.records.values())


def test_the_user_session_holds_ciphertext_and_hashes_only(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    cookie = area.client.cookies[USER_COOKIE]

    [record] = area.sessions.records.values()

    assert cookie not in record.id_hash
    assert b"eyJ" not in record.access_token_enc
    assert replace(record, fresh_auth_at=None).sub == "leo-sub"


# --- wiring -------------------------------------------------------------------------


def _environ(**extra: str) -> dict[str, str]:
    from tests.unit.oidc_support import ENVIRON

    return {"DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db", **ENVIRON, **extra}


def test_the_user_area_is_off_unless_switched_on() -> None:
    from chatmemory.entrypoints import admin

    assert admin.build(_environ()).user_area is None
    on = admin.build(
        _environ(ACCOUNT_PROVISIONING_ENABLED="true", PROVISIONING_EMAIL_KEY="k" * 32)
    )
    assert on.user_area is not None


def test_switched_on_without_the_email_key_refuses_to_start() -> None:
    from chatmemory.entrypoints import admin

    with pytest.raises(admin.MisconfiguredUserArea):
        admin.build(_environ(ACCOUNT_PROVISIONING_ENABLED="true", PROVISIONING_EMAIL_KEY="short"))


def test_switched_on_without_sign_in_stays_off() -> None:
    from chatmemory.entrypoints import admin

    environ = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
        "ACCOUNT_PROVISIONING_ENABLED": "true",
        "PROVISIONING_EMAIL_KEY": "k" * 32,
    }
    assert admin.build(environ).user_area is None


def test_the_stated_retention_comes_from_the_settings_that_enforce_it() -> None:
    from chatmemory.entrypoints.admin import retention_facts

    assert retention_facts({}) == RetentionFacts(
        tracing=False, trace_retention_days=90, memory_retention_days=30, backup_retention_days=None
    )
    configured = retention_facts(
        {
            "TRACING_ENABLED": "true",
            "LANGFUSE_HOST": "https://langfuse.example.com",
            "TRACE_RETENTION_DAYS": "30",
            "BACKUP_RETENTION_DAYS": "7",
        }
    )
    assert configured.tracing and configured.trace_retention_days == 30
    assert configured.backup_retention_days == 7
    assert retention_facts({"TRACING_ENABLED": "true"}).tracing is False, "no Langfuse host"
