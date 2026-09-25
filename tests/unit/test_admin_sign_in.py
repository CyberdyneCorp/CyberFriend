"""CyberdyneAuth sign-in through the real console app, over FakeOIDC.

The browser is a Starlette test client on https (the cookies are `__Host-`
and Secure); the issuer is `FakeOIDC` behind the provider's transport. Every
test drives the same routes, middleware and route table production runs.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from chatmemory.admin.audit import ChangeKind
from chatmemory.admin.auth import SESSION_COOKIE, UNAUTHENTICATED, Principal, Role
from chatmemory.admin.oidc.crypto import TokenCipher, digest
from chatmemory.admin.oidc.provider import TokenResponse
from chatmemory.admin.oidc.service import LOGIN_LIFETIME, SignedIn, SignInFailed
from tests.e2e.harness.oidc import END_SESSION
from tests.unit.oidc_support import (
    CSRF,
    PUBLIC_URL,
    SESSION_KEY,
    Rig,
    browser_sign_in,
    rig,
    start,
)
from tests.unit.test_admin_api import Console, build_console

OPT_OUT = {"platform": "discord", "platform_user_id": 42}


@pytest.fixture
def signing() -> Rig:
    return rig()


@pytest.fixture
async def console(signing: Rig) -> Console:
    return await build_console(sign_in=signing.sign_in)


def _browser(console: Console) -> TestClient:
    """Another browser on the same console: its own cookie jar."""
    return TestClient(console.client.app, base_url=PUBLIC_URL)


def _session_id(client: TestClient) -> str:
    value = client.cookies.get(SESSION_COOKIE)
    assert value, "no session cookie"
    return str(value)


def _set_cookies(response_headers: list[str], name: str) -> list[str]:
    return [h for h in response_headers if h.startswith(f"{name}=")]


# --- starting a sign-in -------------------------------------------------


def test_login_redirects_with_pkce_and_binds_the_browser(console: Console) -> None:
    response = console.client.get("/auth/login", follow_redirects=False)

    location = response.headers["location"]
    assert location.startswith("https://auth.test/authorize?")
    for part in ("code_challenge_method=S256", "code_challenge=", "state=", "nonce=",
                 "redirect_uri=https%3A%2F%2Fadmin.test%2Fauth%2Fcallback"):
        assert part in location
    (cookie,) = _set_cookies(response.headers.get_list("set-cookie"), "__Host-cf_login")
    lowered = cookie.lower()
    for flag in ("httponly", "secure", "samesite=lax", "path=/", "max-age=600"):
        assert flag in lowered


def test_without_sign_in_configured_the_auth_routes_are_not_there() -> None:
    async def build() -> Console:
        return await build_console()

    plain = asyncio.run(build())

    assert plain.client.get("/auth/login", follow_redirects=False).status_code == 404
    assert plain.client.get("/auth/callback?code=x&state=y").status_code == 404


# --- completing it ----------------------------------------------------------


def test_a_successful_sign_in_sets_only_an_opaque_session_cookie(
    console: Console, signing: Rig
) -> None:
    code, state = signing.fake.authorize(start(console.client), "ana-sub")
    response = console.client.get(
        "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"{PUBLIC_URL}/#/status"
    cookies = response.headers.get_list("set-cookie")
    (session,) = _set_cookies(cookies, SESSION_COOKIE)
    for flag in ("httponly", "secure", "samesite=strict", "path=/"):
        assert flag in session.lower()
    # The pre-sign-in cookie is cleared.
    (cleared,) = _set_cookies(cookies, "__Host-cf_login")
    assert 'max-age=0' in cleared.lower() or "expires=" in cleared.lower()
    # No token reaches the browser, in a header or the body.
    sent = response.text + " ".join(f"{k}: {v}" for k, v in response.headers.items())
    assert "eyJ" not in sent


def test_the_session_names_the_person_and_their_role(console: Console, signing: Rig) -> None:
    assert browser_sign_in(console.client, signing.fake, "ana-sub") == 302

    body = console.client.get("/api/session").json()

    assert body == {
        "subject": "ana-sub",
        "display": "ana@cyberdyne.test",
        "roles": ["admin", "operator"],
        "via": "oidc",
    }


def test_the_stored_session_holds_ciphertext_and_hashes_only(
    console: Console, signing: Rig
) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")
    session_id = _session_id(console.client)

    (record,) = signing.sessions.records.values()
    assert record.id_hash == digest(session_id)
    for sealed in (record.access_token_enc, record.refresh_token_enc, record.id_token_enc):
        assert sealed is not None
        assert b"eyJ" not in sealed


def test_a_replayed_state_is_refused(console: Console, signing: Rig) -> None:
    code, state = signing.fake.authorize(start(console.client), "ana-sub")
    params = {"code": code, "state": state}
    first = console.client.get("/auth/callback", params=params, follow_redirects=False)
    assert first.status_code == 302

    again = console.client.get("/auth/callback", params=params, follow_redirects=False)

    assert again.status_code == 400
    assert len(signing.sessions.records) == 1


def test_an_unknown_state_is_refused(console: Console, signing: Rig) -> None:
    code, _ = signing.fake.authorize(start(console.client), "ana-sub")

    response = console.client.get(
        "/auth/callback", params={"code": code, "state": "made-up"}, follow_redirects=False
    )

    assert response.status_code == 400
    assert signing.sessions.records == {}


def test_a_callback_in_another_browser_is_refused(console: Console, signing: Rig) -> None:
    """Login CSRF / session swapping: the callback URL without the binding cookie."""
    code, state = signing.fake.authorize(start(console.client), "ana-sub")

    victim = _browser(console)
    response = victim.get(
        "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
    )

    assert response.status_code == 400
    assert SESSION_COOKIE not in victim.cookies
    assert signing.sessions.records == {}
    # And the state is spent: the right browser cannot finish it afterwards.
    late = console.client.get(
        "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
    )
    assert late.status_code == 400


def test_a_callback_with_a_wrong_binding_cookie_is_refused(
    console: Console, signing: Rig
) -> None:
    code, state = signing.fake.authorize(start(console.client), "ana-sub")
    console.client.cookies.set("__Host-cf_login", "not-the-binding", domain="admin.test")

    response = console.client.get(
        "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
    )

    assert response.status_code == 400
    assert signing.sessions.records == {}


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(lambda f: setattr(f, "omit_nonce", True), id="id-token-nonce-missing"),
        pytest.param(lambda f: f.id_overrides.update(nonce="other"), id="id-token-nonce-wrong"),
        pytest.param(lambda f: f.id_overrides.update(aud="other"), id="id-token-aud-wrong"),
        pytest.param(lambda f: f.access_overrides.update(aud="other"), id="access-aud-wrong"),
        pytest.param(lambda f: f.access_overrides.update(iss="https://x"), id="access-iss-wrong"),
        pytest.param(lambda f: f.access_overrides.update(type="id"), id="access-type-wrong"),
        pytest.param(lambda f: setattr(f, "userinfo_sub", "mallory"), id="userinfo-sub-differs"),
        pytest.param(lambda f: f.id_overrides.update(sub="ben-sub"), id="id-token-sub-differs"),
    ],
)
def test_a_sign_in_breaking_one_rule_creates_no_session(
    console: Console, signing: Rig, tamper: object
) -> None:
    assert callable(tamper)
    tamper(signing.fake)

    status = browser_sign_in(console.client, signing.fake, "ana-sub")

    assert status == 400
    assert signing.sessions.records == {}
    assert SESSION_COOKIE not in console.client.cookies


@pytest.mark.parametrize("sub", ["cass-sub", "dan-sub"], ids=["no-roles-claim", "neither-role"])
def test_no_console_role_means_no_session_and_says_only_no_access(
    console: Console, signing: Rig, sub: str
) -> None:
    code, state = signing.fake.authorize(start(console.client), sub)
    response = console.client.get(
        "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
    )

    assert response.status_code == 403
    assert "No access." in response.text
    assert sub not in response.text
    assert signing.sessions.records == {}


# --- what a session may do ----------------------------------------------------


def test_an_operator_reads_and_cannot_change(console: Console, signing: Rig) -> None:
    browser_sign_in(console.client, signing.fake, "ben-sub")

    assert console.client.get("/api/status").status_code == 200
    response = console.client.post("/api/optouts", json=OPT_OUT, headers=CSRF)

    assert response.status_code == 403
    assert response.json() == {"error": "requires admin"}
    assert console.optouts.purged == []


def test_an_admin_change_is_recorded_as_oidc_sub_with_the_email(
    console: Console, signing: Rig
) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")

    response = console.client.post("/api/optouts", json=OPT_OUT, headers=CSRF)

    assert response.status_code == 200, response.text
    (entry,) = [e for e in console.changes._entries if e.kind is ChangeKind.APPLIED]  # noqa: SLF001
    assert entry.operator == "oidc:ana-sub"
    assert entry.operator_display == "ana@cyberdyne.test"
    audit = console.client.get("/api/audit").json()
    assert audit[0]["operator"] == "oidc:ana-sub"
    assert audit[0]["operator_display"] == "ana@cyberdyne.test"


def test_a_setting_edited_after_sign_in_carries_the_email(
    console: Console, signing: Rig
) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")

    response = console.client.put(
        "/api/settings/ask_min_confidence", json={"value": "0.7"}, headers=CSRF
    )

    assert response.status_code == 200, response.text
    assert console.store.rows[-1].updated_by == "oidc:ana-sub"
    assert console.store.displays == ["ana@cyberdyne.test"]


# --- CSRF -------------------------------------------------------------------


def test_a_cookie_write_without_the_console_header_is_refused(
    console: Console, signing: Rig
) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")

    response = console.client.post("/api/optouts", json=OPT_OUT)

    assert response.status_code == 403
    assert response.json() == {"error": "cross-site request refused"}
    assert console.optouts.purged == []


def test_a_cookie_write_from_another_origin_is_refused(console: Console, signing: Rig) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")

    response = console.client.post(
        "/api/optouts", json=OPT_OUT, headers={**CSRF, "Origin": "https://evil.test"}
    )

    assert response.status_code == 403
    assert console.optouts.purged == []


def test_a_cookie_write_from_the_console_origin_is_served(
    console: Console, signing: Rig
) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")

    response = console.client.post(
        "/api/optouts", json=OPT_OUT, headers={**CSRF, "Origin": PUBLIC_URL}
    )

    assert response.status_code == 200


def test_every_response_carries_the_security_headers(console: Console) -> None:
    for response in (console.client.get("/health"), console.client.get("/api/status")):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        csp = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp
        assert "form-action 'self' https://auth.test" in csp


# --- a bearer header wins -------------------------------------------------------


def test_bearer_with_a_valid_cookie_is_served_as_the_bearer_and_the_session_untouched(
    console: Console, signing: Rig
) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")
    (before,) = signing.sessions.records.values()
    signing.clock.advance(timedelta(seconds=5))

    session = console.client.get("/api/session", headers=console.auth()).json()
    write = console.client.post("/api/optouts", json=OPT_OUT, headers={**console.auth(), **CSRF})

    assert session["via"] == "token" and session["subject"] == "ana"
    # Sign-in is configured, so the token is operator even though the cookie is admin.
    assert write.status_code == 403
    (after,) = signing.sessions.records.values()
    assert after == before


def test_an_invalid_bearer_with_a_valid_cookie_is_refused(console: Console, signing: Rig) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")

    response = console.client.get(
        "/api/status", headers={"Authorization": "Bearer cfa_not-a-real-one"}
    )

    assert response.status_code == 401
    assert response.content == b'{"error":"unauthorized"}'


def test_a_repeated_session_cookie_is_refused(console: Console, signing: Rig) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")
    session_id = _session_id(console.client)

    fresh = _browser(console)
    response = fresh.get(
        "/api/status",
        headers={"Cookie": f"{SESSION_COOKIE}={session_id}; {SESSION_COOKIE}=other"},
    )

    assert response.status_code == 401


# --- signing out ------------------------------------------------------------


def test_logout_needs_the_console_header(console: Console, signing: Rig) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")

    assert console.client.post("/auth/logout").status_code == 403
    assert console.client.get("/api/status").status_code == 200


def test_logout_ends_the_session_and_sends_the_browser_to_end_session(
    console: Console, signing: Rig
) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")
    session_id = _session_id(console.client)

    response = console.client.post("/auth/logout", headers=CSRF)

    assert response.status_code == 200
    url = response.json()["end_session_url"]
    assert url.startswith(END_SESSION + "?")
    assert "id_token_hint=eyJ" in url
    assert "post_logout_redirect_uri=https%3A%2F%2Fadmin.test%2F" in url
    assert len(signing.fake.revoked) == 1
    # The cookie stops working at once, even if a copy of it is replayed.
    replay = _browser(console)
    replay_status = replay.get(
        "/api/status", headers={"Cookie": f"{SESSION_COOKIE}={session_id}"}
    ).status_code
    assert replay_status == 401


# --- refresh ---------------------------------------------------------------


def _signed_in_near_expiry(console: Console, signing: Rig, sub: str) -> None:
    signing.fake.access_ttl = 30  # inside the 60-second refresh window
    assert browser_sign_in(console.client, signing.fake, sub) == 302
    signing.fake.access_ttl = 900


def _revoked(signing: Rig) -> bool:
    (record,) = signing.sessions.records.values()
    return record.revoked_at is not None


def test_a_session_near_expiry_is_refreshed_before_the_request(
    console: Console, signing: Rig
) -> None:
    _signed_in_near_expiry(console, signing, "ana-sub")

    assert console.client.get("/api/status").status_code == 200
    assert console.client.get("/api/status").status_code == 200

    assert signing.fake.grants == ["authorization_code", "refresh_token"]


def test_a_refreshed_token_without_a_roles_claim_ends_the_session_as_401(
    console: Console, signing: Rig
) -> None:
    _signed_in_near_expiry(console, signing, "ana-sub")
    signing.fake.users["ana-sub"].roles = None

    response = console.client.get("/api/status")

    assert response.status_code == 401
    assert _revoked(signing)


def test_a_refreshed_token_with_neither_role_ends_the_session_as_403(
    console: Console, signing: Rig
) -> None:
    _signed_in_near_expiry(console, signing, "ana-sub")
    signing.fake.users["ana-sub"].roles = ["other-client:admin"]

    response = console.client.get("/api/status")

    assert response.status_code == 403
    assert response.json() == {"error": "no console access"}
    assert _revoked(signing)
    assert console.client.get("/api/status").status_code == 401


def test_an_admin_lowered_to_operator_continues_as_operator(
    console: Console, signing: Rig
) -> None:
    _signed_in_near_expiry(console, signing, "ana-sub")
    signing.fake.users["ana-sub"].roles = [signing.fake.role("operator")]

    assert console.client.get("/api/session").json()["roles"] == ["operator"]
    response = console.client.post("/api/optouts", json=OPT_OUT, headers=CSRF)

    assert response.status_code == 403
    assert not _revoked(signing)


def test_a_failed_refresh_ends_the_session(console: Console, signing: Rig) -> None:
    _signed_in_near_expiry(console, signing, "ana-sub")
    signing.fake.client_secret = "rotated"  # the token endpoint now refuses us

    assert console.client.get("/api/status").status_code == 401
    assert _revoked(signing)


def test_a_session_idle_for_twelve_hours_has_ended(console: Console, signing: Rig) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")
    signing.clock.advance(timedelta(hours=12, seconds=1))

    assert console.client.get("/api/status").status_code == 401


async def test_two_concurrent_requests_near_expiry_trigger_exactly_one_refresh(
    signing: Rig,
) -> None:
    signing.fake.access_ttl = 30
    begun = await signing.sign_in.begin()
    code, state = signing.fake.authorize(begun.authorization_url, "ana-sub")
    result = await signing.sign_in.complete(state=state, code=code, binding=begun.binding)
    assert isinstance(result, SignedIn)
    signing.fake.access_ttl = 900

    first, second = await asyncio.gather(
        signing.sign_in.authenticate(result.session_id),
        signing.sign_in.authenticate(result.session_id),
    )

    assert signing.fake.grants.count("refresh_token") == 1
    for principal in (first, second):
        assert isinstance(principal, Principal)
        assert principal.has(Role.ADMIN)


async def test_a_bearer_request_reaches_the_handler_without_its_cookies(signing: Rig) -> None:
    """The session is not merely ignored: nothing downstream can read the cookie."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from chatmemory.admin.access import Access, RouteAccess
    from chatmemory.admin.auth import (
        AdminAuthenticator,
        AdminAuthMiddleware,
        InMemoryOperatorTokens,
        Operator,
    )

    async def echo(request: Request) -> JSONResponse:
        return JSONResponse({"cookie": request.headers.get("cookie")})

    tokens = InMemoryOperatorTokens()
    issued = await tokens.issue(Operator("ana"))
    app = Starlette(routes=[Route("/api/echo", echo, methods=["GET"])])
    app.add_middleware(
        AdminAuthMiddleware,
        authenticator=AdminAuthenticator(tokens, oidc_configured=True, sessions=signing.sign_in),
        access=RouteAccess({("GET", "/api/echo"): Access.OPERATOR}, app.router.routes),
    )

    response = TestClient(app, base_url=PUBLIC_URL).get(
        "/api/echo",
        headers={"Authorization": f"Bearer {issued.token}", "Cookie": f"{SESSION_COOKIE}=x"},
    )

    assert response.status_code == 200
    assert response.json() == {"cookie": None}


async def _signed_in(signing: Rig) -> str:
    """A sign-in straight through the service; the session id."""
    begun = await signing.sign_in.begin()
    code, state = signing.fake.authorize(begun.authorization_url, "ana-sub")
    result = await signing.sign_in.complete(state=state, code=code, binding=begun.binding)
    assert isinstance(result, SignedIn)
    return result.session_id


def _store_access_token(signing: Rig, token: str) -> None:
    """Put `token` in the one session, sealed as the service would seal it."""
    ((id_hash, record),) = signing.sessions.records.items()
    sealed = TokenCipher(SESSION_KEY).seal(token, context=f"access:{id_hash}")
    signing.sessions.records[id_hash] = replace(record, access_token_enc=sealed)


def test_a_session_refreshes_again_with_the_rotated_refresh_token(
    console: Console, signing: Rig
) -> None:
    """Reuse detection: the second refresh must present the token the first
    one was given, or CyberdyneAuth revokes the family."""
    _signed_in_near_expiry(console, signing, "ana-sub")
    assert console.client.get("/api/status").status_code == 200

    signing.clock.advance(timedelta(seconds=signing.fake.access_ttl - 30))
    assert console.client.get("/api/status").status_code == 200
    signing.clock.advance(timedelta(seconds=signing.fake.access_ttl - 30))
    assert console.client.get("/api/status").status_code == 200

    assert signing.fake.grants == [
        "authorization_code",
        "refresh_token",
        "refresh_token",
        "refresh_token",
    ]
    assert not _revoked(signing)


async def test_a_sign_out_during_a_refresh_leaves_nothing_live(
    signing: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    signing.fake.access_ttl = 30
    session_id = await _signed_in(signing)
    provider = signing.sign_in.provider
    refresh = provider.refresh

    async def refresh_while_signing_out(token: str) -> TokenResponse:
        tokens = await refresh(token)
        await signing.sign_in.sign_out(session_id)
        return tokens

    monkeypatch.setattr(provider, "refresh", refresh_while_signing_out)
    (before,) = signing.sessions.records.values()

    result = await signing.sign_in.authenticate(session_id)

    assert result is UNAUTHENTICATED
    # Sign-out revoked the spent token; the refresh revoked the one it was given.
    assert len(signing.fake.revoked) == 2
    assert signing.fake.revoked[0] != signing.fake.revoked[1]
    (after,) = signing.sessions.records.values()
    assert after.revoked_at is not None
    assert after.refresh_token_enc == before.refresh_token_enc


def test_a_bearer_write_from_another_origin_is_refused(console: Console, signing: Rig) -> None:
    response = console.client.post(
        "/api/optouts",
        json=OPT_OUT,
        headers={**console.auth(), "Origin": "https://evil.test"},
    )

    assert response.status_code == 403
    assert response.json() == {"error": "cross-site request refused"}


def test_a_repeated_session_cookie_is_refused_whichever_copy_is_valid(
    console: Console, signing: Rig
) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")
    session_id = _session_id(console.client)

    response = _browser(console).get(
        "/api/status",
        headers={"Cookie": f"{SESSION_COOKIE}=other; {SESSION_COOKIE}={session_id}"},
    )

    assert response.status_code == 401


def test_a_refresh_for_another_subject_ends_the_session(
    console: Console, signing: Rig
) -> None:
    _signed_in_near_expiry(console, signing, "ana-sub")
    signing.fake.access_overrides["sub"] = "ben-sub"

    assert console.client.get("/api/status").status_code == 401
    assert _revoked(signing)


def test_a_refreshed_id_token_for_another_subject_ends_the_session(
    console: Console, signing: Rig
) -> None:
    _signed_in_near_expiry(console, signing, "ana-sub")
    signing.fake.id_token_on_refresh = True
    signing.fake.id_overrides["sub"] = "ben-sub"

    assert console.client.get("/api/status").status_code == 401
    assert _revoked(signing)


def test_a_refreshed_id_token_for_the_same_subject_replaces_the_stored_one(
    console: Console, signing: Rig
) -> None:
    _signed_in_near_expiry(console, signing, "ana-sub")
    (before,) = signing.sessions.records.values()
    signing.fake.id_token_on_refresh = True

    assert console.client.get("/api/status").status_code == 200
    (after,) = signing.sessions.records.values()
    assert after.id_token_enc != before.id_token_enc


def test_a_stored_access_token_for_another_subject_is_refused(
    console: Console, signing: Rig
) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")
    _store_access_token(signing, signing.fake.mint_access("ben-sub"))

    assert console.client.get("/api/status").status_code == 401


@pytest.mark.parametrize(
    "roles", [None, ["other-client:admin"]], ids=["no-roles-claim", "neither-role"]
)
def test_a_stored_access_token_without_a_console_role_is_refused(
    console: Console, signing: Rig, roles: list[str] | None
) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")
    _store_access_token(signing, signing.fake.mint_access("ana-sub", roles=roles))

    assert console.client.get("/api/status").status_code == 401


async def test_a_login_purpose_refusal_is_the_state_rule(signing: Rig) -> None:
    begun = await signing.sign_in.begin()
    code, state = signing.fake.authorize(begun.authorization_url, "ana-sub")
    ((state_hash, (login, used)),) = signing.logins.records.items()
    signing.logins.records[state_hash] = (replace(login, purpose="link"), used)

    result = await signing.sign_in.complete(state=state, code=code, binding=begun.binding)

    assert result == SignInFailed("state")


def test_a_login_older_than_ten_minutes_is_refused(console: Console, signing: Rig) -> None:
    code, state = signing.fake.authorize(start(console.client), "ana-sub")
    signing.clock.advance(LOGIN_LIFETIME + timedelta(seconds=1))

    response = console.client.get(
        "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
    )

    assert response.status_code == 400
    assert signing.sessions.records == {}


def test_a_used_login_is_dropped_when_the_next_begins(console: Console, signing: Rig) -> None:
    browser_sign_in(console.client, signing.fake, "ana-sub")
    start(_browser(console))

    ((_, (_, used)),) = signing.logins.records.items()
    assert used is None


def test_a_token_response_that_is_not_bearer_creates_no_session(
    console: Console, signing: Rig
) -> None:
    signing.fake.token_type = "mac"

    assert browser_sign_in(console.client, signing.fake, "ana-sub") == 400
    assert signing.sessions.records == {}


def test_each_request_keeps_a_session_from_going_idle(console: Console, signing: Rig) -> None:
    signing.fake.access_ttl = 2 * 86400  # no refresh: only the request marks it seen
    browser_sign_in(console.client, signing.fake, "ana-sub")

    signing.clock.advance(timedelta(hours=11))
    assert console.client.get("/api/status").status_code == 200
    signing.clock.advance(timedelta(hours=11))

    assert console.client.get("/api/status").status_code == 200
