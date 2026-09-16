"""Authentication: the credential decides the viewer, and nothing else can.

These tests are the ones that matter if the endpoint is reachable from the
internet. They assert the negative property -- that no header, parameter or
body field offers a way to choose whose view you get -- which means most of
them are attempts that must fail rather than requests that must succeed.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from chatmemory.app.tokens import InMemoryTokenStore, generate_token
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.mcp.auth import (
    Authenticator,
    BearerAuthMiddleware,
    Unauthenticated,
    bearer_token,
    current_viewer,
)

ALICE = PersonRef("discord", 1001)
BOB = PersonRef("discord", 1002)
GENERAL = ChannelRef("discord", 100)
LEADERSHIP = ChannelRef("discord", 300)


class FakeAcl:
    """Resolves each person to their own channels, and strangers to none."""

    def __init__(self, visibility: dict[PersonRef, frozenset[ChannelRef]]) -> None:
        self._visibility = visibility

    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        return Viewer(
            person=person,
            visible_channels=self._visibility.get(person, frozenset()),
        )


def acl() -> FakeAcl:
    return FakeAcl(
        {
            ALICE: frozenset({GENERAL}),
            BOB: frozenset({GENERAL, LEADERSHIP}),
        }
    )


# --- header parsing ----------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic abc", "Token abc", "  "],
)
def test_non_bearer_headers_yield_no_token(header: str | None) -> None:
    assert bearer_token(header) is None


def test_the_scheme_is_matched_case_insensitively() -> None:
    """Clients send `bearer`, `Bearer` and `BEARER`; all are the same scheme."""
    assert bearer_token("bearer cfm_x") == "cfm_x"
    assert bearer_token("BEARER cfm_x") == "cfm_x"
    assert bearer_token("Bearer  cfm_x  ") == "cfm_x"


# --- the authenticator -------------------------------------------------


async def test_a_valid_token_yields_that_person_s_viewer() -> None:
    store = InMemoryTokenStore()
    issued = await store.issue(ALICE)
    viewer = await Authenticator(store, acl()).viewer_for_token(issued.token)
    assert viewer is not None
    assert viewer.person == ALICE
    assert viewer.visible_channels == frozenset({GENERAL})


async def test_an_unknown_token_yields_no_viewer() -> None:
    authenticator = Authenticator(InMemoryTokenStore(), acl())
    assert await authenticator.viewer_for_token(generate_token()) is None


async def test_permissions_come_from_the_acl_not_the_token() -> None:
    """A token carries identity, never authorisation.

    If it carried authorisation, revoking a Discord role would leave the old
    access minted into an outstanding credential.
    """
    store = InMemoryTokenStore()
    issued = await store.issue(BOB)
    narrowed = FakeAcl({BOB: frozenset()})
    viewer = await Authenticator(store, narrowed).viewer_for_token(issued.token)
    assert viewer is not None
    assert viewer.visible_channels == frozenset()


async def test_a_person_with_no_visible_channels_still_authenticates() -> None:
    """Authenticated-with-nothing and unauthenticated are different states.

    Conflating them would answer "who are you?" with "nothing to see", which
    is how a permission bug gets reported as a login bug for a week.
    """
    store = InMemoryTokenStore()
    stranger = PersonRef("discord", 9999)
    issued = await store.issue(stranger)
    viewer = await Authenticator(store, acl()).viewer_for_token(issued.token)
    assert viewer is not None
    assert viewer.visible_channels == frozenset()


# --- the middleware ----------------------------------------------------


def build_probe(authenticator: Authenticator) -> TestClient:
    """An app that reports whichever viewer the middleware bound."""

    async def whoami(_: Request) -> JSONResponse:
        viewer = current_viewer()
        return JSONResponse(
            {
                "person": str(viewer.person),
                "channels": sorted(c.platform_channel_id for c in viewer.visible_channels),
            }
        )

    async def open_route(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[Route("/mcp", whoami), Route("/health", open_route)]
    )
    return TestClient(BearerAuthMiddleware(app, authenticator))


async def _client_for(person: PersonRef) -> tuple[TestClient, str]:
    store = InMemoryTokenStore()
    issued = await store.issue(person)
    return build_probe(Authenticator(store, acl())), issued.token


async def test_a_credentialled_call_is_bound_to_its_own_person() -> None:
    client, token = await _client_for(ALICE)
    body = client.get("/mcp", headers={"Authorization": f"Bearer {token}"}).json()
    assert body["person"] == str(ALICE)
    assert body["channels"] == [GENERAL.platform_channel_id]


async def test_a_call_with_no_credential_is_refused() -> None:
    client, _ = await _client_for(ALICE)
    response = client.get("/mcp")
    assert response.status_code == 401
    assert "www-authenticate" in response.headers


async def test_a_prefixed_mount_still_refuses_an_anonymous_call() -> None:
    """The guard has to answer the same question the router will.

    Starlette resolves routes on the path with `root_path` stripped, so behind
    a proxy or a mount that sets one, a guard reading the raw path stops
    matching while the router keeps dispatching -- and the corpus surface runs
    with no viewer bound, which is the one thing retrieval never does.
    """
    client, _ = await _client_for(ALICE)
    behind = TestClient(client.app, root_path="/agent")

    response = behind.get("/agent/mcp")

    assert response.status_code == 401
    assert "www-authenticate" in response.headers


async def test_a_credential_is_still_bound_behind_a_prefixed_mount() -> None:
    client, token = await _client_for(ALICE)
    behind = TestClient(client.app, root_path="/agent")

    body = behind.get("/agent/mcp", headers={"Authorization": f"Bearer {token}"}).json()

    assert body["person"] == str(ALICE)


async def test_missing_and_invalid_credentials_are_answered_identically() -> None:
    """The endpoint must not double as an oracle for which tokens exist."""
    client, _ = await _client_for(ALICE)
    absent = client.get("/mcp")
    invalid = client.get("/mcp", headers={"Authorization": f"Bearer {generate_token()}"})
    malformed = client.get("/mcp", headers={"Authorization": "Basic hunter2"})
    assert absent.status_code == invalid.status_code == malformed.status_code == 401
    assert absent.content == invalid.content == malformed.content


async def test_a_revoked_credential_stops_working_immediately() -> None:
    store = InMemoryTokenStore()
    issued = await store.issue(ALICE)
    client = build_probe(Authenticator(store, acl()))
    header = {"Authorization": f"Bearer {issued.token}"}
    assert client.get("/mcp", headers=header).status_code == 200
    await store.revoke(ALICE)
    assert client.get("/mcp", headers=header).status_code == 401


async def test_health_is_reachable_without_a_credential() -> None:
    """An orchestrator's probe holds no token and exposes no content."""
    client, _ = await _client_for(ALICE)
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        "X-Viewer",
        "X-Viewer-Id",
        "X-On-Behalf-Of",
        "X-Person-Id",
        "X-Discord-User-Id",
        "X-Forwarded-User",
        "X-Impersonate",
    ],
)
async def test_no_header_can_select_a_different_viewer(header: str) -> None:
    """Alice's token, Bob named in every header we could think of."""
    client, token = await _client_for(ALICE)
    body = client.get(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            header: str(BOB.platform_user_id),
        },
    ).json()
    assert body["person"] == str(ALICE)
    assert body["channels"] == [GENERAL.platform_channel_id]


async def test_query_parameters_cannot_select_a_viewer() -> None:
    client, token = await _client_for(ALICE)
    body = client.get(
        f"/mcp?viewer={BOB.platform_user_id}&person_id={BOB.platform_user_id}",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert body["person"] == str(ALICE)


async def test_two_credentials_on_one_request_are_refused() -> None:
    """An ambiguous caller is refused, never guessed at.

    Reading a repeated `Authorization` with a dict takes the *last* value, so
    a request bearing two credentials would be served as whichever one we
    happened to read -- and a proxy in front that reads the first would
    disagree with us about who called. This test caught exactly that.
    """
    store = InMemoryTokenStore()
    alice = await store.issue(ALICE)
    bob = await store.issue(BOB)
    client = build_probe(Authenticator(store, acl()))
    response = client.get(
        "/mcp",
        headers=[
            ("Authorization", f"Bearer {alice.token}"),
            ("Authorization", f"Bearer {bob.token}"),
        ],
    )
    assert response.status_code == 401


def test_current_viewer_refuses_to_invent_one() -> None:
    """Outside an authenticated request there is no viewer, and no default."""
    with pytest.raises(Unauthenticated):
        current_viewer()
