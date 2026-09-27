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
from chatmemory.admin.oidc.crypto import TokenCipher
from chatmemory.admin.oidc.service import SignInFailed
from chatmemory.admin.oidc.store import LoginRecord
from chatmemory.admin.server import build_app
from chatmemory.admin.user.auth import USER_COOKIE
from chatmemory.admin.user.routes import ACCOUNT_NOT_DELETED, NOT_LINKED, UserArea
from chatmemory.admin.user.service import UserSignIn
from chatmemory.admin.user.store import InMemoryUserSessionStore
from chatmemory.app.accounts import (
    AccountLinking,
    code_digest,
    discord_label,
    email_hmac,
    mask_email,
)
from chatmemory.app.feature_requests import FeatureRequestService
from chatmemory.app.privacy import PrivacyService, RetentionFacts
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.accounts import DiscordProfile, LinkOutcome, NewLink
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
from tests.unit.oidc_support import CSRF, PUBLIC_URL, SESSION_KEY, Rig, rig
from tests.unit.test_admin_api import build_console

KEY = b"k" * 32
LEO = PersonRef("discord", 7)
ANA = PersonRef("discord", 8)
LEO_EMAIL = "leo@cyberdyne.test"
CODE = "leo-link-code"
SAME_ORIGIN = {"Origin": PUBLIC_URL}
NAMES = {LEO: "Leo", ANA: "Ana <b>"}


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

    async def code_holder(self, code_sha256: bytes, now: datetime) -> DiscordProfile | None:
        code = self.codes.get(code_sha256)
        if code is None or code.used or code.expires_at <= now:
            return None
        return DiscordProfile(code.person, NAMES[code.person])

    async def linked_person(self, sub: str) -> PersonRef | None:
        return self.links.get(sub)

    async def linked_profile(self, sub: str) -> DiscordProfile | None:
        person = self.links.get(sub)
        return None if person is None else DiscordProfile(person, NAMES[person])

    async def unlink(self, person: PersonRef) -> bool:
        subs = [s for s, p in self.links.items() if p == person]
        for sub in subs:
            del self.links[sub]
        return bool(subs)


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
    sign_in_service: UserSignIn

    def browser(self) -> TestClient:
        return TestClient(self.client.app, base_url=PUBLIC_URL)

    def follow_link(self, browser: TestClient, sub: str, code: str = CODE) -> int:
        page = browser.get("/link", params={"code": code}, follow_redirects=False)
        assert page.status_code == 200, page.text
        started = browser.post(
            "/link", data={"code": code}, headers=SAME_ORIGIN, follow_redirects=False
        )
        assert started.status_code == 303, started.text
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
    user_sign_in = UserSignIn(
        signing.sign_in,
        sessions,
        AccountLinking(accounts, email_key=KEY, clock=signing.clock),  # type: ignore[arg-type]
        clock=signing.clock,
    )
    user_area = UserArea(
        sign_in=user_sign_in,
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
        user_sign_in,
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
    assert session == {
        "email": LEO_EMAIL,
        "linked": True,
        "discord": "Leo (Discord user 7)",
        "fresh": False,
    }


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
    started = area.client.post(
        "/link", data={"code": CODE}, headers=SAME_ORIGIN, follow_redirects=False
    )
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

    assert area.browser().get("/link", params={"code": CODE}).status_code == 400
    assert _link_without_page(area, area.browser(), "leo-sub") == 403
    assert area.accounts.links == {}


def test_an_expired_code_is_refused(area: Area) -> None:
    area.signing.clock.advance(timedelta(minutes=16))

    assert area.client.get("/link", params={"code": CODE}).status_code == 400
    assert _link_without_page(area, area.client, "leo-sub") == 403
    assert area.accounts.links == {}


def _link_without_page(area: Area, browser: TestClient, sub: str) -> int:
    """The form posted straight away, as a code that expired on its page would be."""
    started = browser.post(
        "/link", data={"code": CODE}, headers=SAME_ORIGIN, follow_redirects=False
    )
    assert started.status_code == 303, started.text
    oidc_code, state = area.signing.fake.authorize(started.headers["location"], sub)
    return browser.get(
        "/auth/callback", params={"code": oidc_code, "state": state}, follow_redirects=False
    ).status_code


def test_a_link_without_a_code_starts_nothing(area: Area) -> None:
    response = area.client.get("/link", follow_redirects=False)
    assert response.status_code == 400
    assert area.signing.logins.records == {}


def test_the_link_page_names_the_discord_account_and_starts_nothing(area: Area) -> None:
    """Someone sent another person's link sees whose it is before signing in."""
    page = area.client.get("/link", params={"code": CODE}, follow_redirects=False)

    assert page.status_code == 200
    assert "Leo (Discord user 7)" in page.text
    assert "Continue only if you asked for this link yourself" in page.text
    assert 'method="post" action="/link"' in page.text
    assert page.headers["cache-control"] == "no-store"
    assert area.signing.logins.records == {}
    assert "__Host-cf_login" not in area.client.cookies
    assert not area.accounts.codes[code_digest(CODE)].used


def test_the_link_page_escapes_the_name(area: Area) -> None:
    expires = area.signing.clock.now + timedelta(minutes=15)
    area.accounts.issue(ANA, "ana-code", "ana@cyberdyne.test", expires)

    page = area.client.get("/link", params={"code": "ana-code"})

    assert "Ana &lt;b&gt; (Discord user 8)" in page.text
    assert "<b>" not in page.text


def test_an_unknown_code_gets_no_page(area: Area) -> None:
    page = area.client.get("/link", params={"code": "nope"})
    assert page.status_code == 400
    assert area.signing.logins.records == {}


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-origin"),
        pytest.param({"Origin": "https://evil.test"}, id="other-origin"),
        pytest.param({"Origin": "null"}, id="opaque-origin"),
        pytest.param({**SAME_ORIGIN, "Sec-Fetch-Site": "cross-site"}, id="cross-site-fetch"),
    ],
)
def test_the_link_form_starts_a_sign_in_only_from_this_origin(
    area: Area, headers: dict[str, str]
) -> None:
    """A cross-site form cannot skip the page that names the Discord account."""
    response = area.client.post(
        "/link", data={"code": CODE}, headers=headers, follow_redirects=False
    )

    assert response.status_code == 403
    assert area.signing.logins.records == {}


def test_the_link_form_without_a_code_starts_nothing(area: Area) -> None:
    response = area.client.post("/link", data={}, headers=SAME_ORIGIN, follow_redirects=False)
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
    assert client.post("/link", data={"code": "x"}, headers=SAME_ORIGIN).status_code == 404
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


# --- a link made by somebody else's code -----------------------------------------


def test_a_link_to_someone_elses_discord_shows_whose_and_can_be_unlinked_on_the_web(
    area: Area,
) -> None:
    """Ana ran /account with Leo's email and sent Leo her link. Leo's /me names
    Ana's Discord account, and Leo can undo the link from his own side."""
    area.accounts.issue(ANA, "ana-code", LEO_EMAIL, _in_15(area))
    page = area.client.get("/link", params={"code": "ana-code"})
    assert "Discord user 8" in page.text
    assert area.follow_link(area.client, "leo-sub", code="ana-code") == 302
    cookie = area.client.cookies[USER_COOKIE]

    shown = area.client.get("/me/session").json()
    unlinked = area.client.post("/me/unlink", headers=CSRF)

    assert shown["discord"] == "Ana <b> (Discord user 8)"
    assert unlinked.status_code == 200 and unlinked.json() == {"unlinked": True}
    assert area.accounts.links == {}
    assert area.signing.fake.revoked, "the refresh token was not revoked at the issuer"
    cleared = [h for h in unlinked.headers.get_list("set-cookie") if h.startswith(USER_COOKIE)]
    assert cleared and "Max-Age=0" in cleared[0]
    replay = area.browser().get("/me/session", headers={"Cookie": f"{USER_COOKIE}={cookie}"})
    assert replay.status_code == 401
    # Leo's own link now works.
    assert area.follow_link(area.browser(), "leo-sub") == 302
    assert area.accounts.links == {"leo-sub": LEO}


def test_unlinking_on_the_web_needs_the_console_header(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")

    cross_site = area.client.post("/me/unlink")
    other_origin = area.client.post(
        "/me/unlink", headers={**CSRF, "Origin": "https://evil.test"}
    )

    assert cross_site.status_code == other_origin.status_code == 403
    assert area.accounts.links == {"leo-sub": LEO}


def test_unlinking_an_unlinked_account_says_so(area: Area) -> None:
    assert area.sign_in(area.client, "mallory-sub") == 302

    response = area.client.post("/me/unlink", headers=CSRF)

    assert response.status_code == 404
    assert response.json() == {"error": NOT_LINKED}


def test_the_refused_page_says_where_to_unlink(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    area.accounts.issue(ANA, "ana-code", LEO_EMAIL, _in_15(area))

    browser = area.browser()
    started = browser.post(
        "/link", data={"code": "ana-code"}, headers=SAME_ORIGIN, follow_redirects=False
    )
    oidc_code, state = area.signing.fake.authorize(started.headers["location"], "leo-sub")
    refused = browser.get(
        "/auth/callback", params={"code": oidc_code, "state": state}, follow_redirects=False
    )

    assert refused.status_code == 403
    assert "press Unlink there" in refused.text
    assert area.accounts.links == {"leo-sub": LEO}


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("Leo", "Leo (Discord user 7)"),
        ("7", "Discord user 7"),
        ("", "Discord user 7"),
        ("(erased)", "Discord user 7"),
    ],
)
def test_the_discord_account_is_named_by_its_id_and_any_real_name(name: str, label: str) -> None:
    assert discord_label(DiscordProfile(LEO, name)) == label


# --- the session's own checks ------------------------------------------------------


def test_the_user_cookie_is_host_only_secure_http_only_and_strict(area: Area) -> None:
    page = area.client.post(
        "/link", data={"code": CODE}, headers=SAME_ORIGIN, follow_redirects=False
    )
    oidc_code, state = area.signing.fake.authorize(page.headers["location"], "leo-sub")
    done = area.client.get(
        "/auth/callback", params={"code": oidc_code, "state": state}, follow_redirects=False
    )

    [cookie] = [h for h in done.headers.get_list("set-cookie") if h.startswith(USER_COOKIE)]
    attributes = {part.strip().lower() for part in cookie.split(";")}
    assert {"samesite=strict", "secure", "httponly", "path=/"} <= attributes
    assert not any(a.startswith("domain=") for a in attributes)


def test_an_auth_time_in_the_future_is_not_fresh(area: Area) -> None:
    """An issuer clock far ahead must not buy a "fresh" sign-in that lasts."""
    area.follow_link(area.client, "leo-sub")
    ahead = int((area.signing.clock.now + timedelta(minutes=10)).timestamp())
    area.signing.fake.id_overrides = {"auth_time": ahead}

    assert area.sign_in(area.client, "leo-sub", "/auth/user/fresh") == 400
    assert area.client.get("/me/session").json()["fresh"] is False


def test_an_auth_time_within_the_clock_skew_is_fresh(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    ahead = int((area.signing.clock.now + timedelta(seconds=30)).timestamp())
    area.signing.fake.id_overrides = {"auth_time": ahead}

    assert area.sign_in(area.client, "leo-sub", "/auth/user/fresh") == 302


def _in_15(area: Area) -> datetime:
    return area.signing.clock.now + timedelta(minutes=15)


def _near_expiry(area: Area) -> None:
    area.signing.fake.access_ttl = 30  # inside the 60-second refresh window
    assert area.follow_link(area.client, "leo-sub") == 302
    area.signing.fake.access_ttl = 900


def test_a_refresh_is_used_for_a_session_near_expiry(area: Area) -> None:
    _near_expiry(area)

    assert area.client.get("/me/session").status_code == 200
    assert area.signing.fake.grants[-1] == "refresh_token"


def test_a_refreshed_id_token_for_another_subject_ends_the_user_session(area: Area) -> None:
    _near_expiry(area)
    area.signing.fake.id_token_on_refresh = True
    area.signing.fake.id_overrides = {"sub": "mallory-sub"}

    assert area.client.get("/me/session").status_code == 401
    [record] = area.sessions.records.values()
    assert record.revoked_at is not None


def test_a_refreshed_access_token_for_another_subject_ends_the_user_session(
    area: Area,
) -> None:
    _near_expiry(area)
    area.signing.fake.access_overrides = {"sub": "mallory-sub"}

    assert area.client.get("/me/session").status_code == 401
    [record] = area.sessions.records.values()
    assert record.revoked_at is not None


def test_a_stored_access_token_for_another_subject_is_refused(area: Area) -> None:
    area.follow_link(area.client, "leo-sub")
    ((id_hash, record),) = area.sessions.records.items()
    other = area.signing.fake.mint_access("mallory-sub")
    sealed = TokenCipher(SESSION_KEY).seal(other, context=f"user-access:{id_hash}")
    area.sessions.records[id_hash] = replace(record, access_token_enc=sealed)

    assert area.client.get("/me/session").status_code == 401


async def test_a_console_login_is_not_finished_as_a_user_sign_in(area: Area) -> None:
    started = await area.signing.sign_in.begin(purpose="admin")
    [(login, _)] = area.signing.logins.records.values()
    assert isinstance(login, LoginRecord) and login.purpose == "admin"

    code, _ = area.signing.fake.authorize(started.authorization_url, "leo-sub")

    result = await area.sign_in_service.complete(login, code=code, binding=started.binding)

    assert isinstance(result, SignInFailed)
    assert area.sessions.records == {}


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
