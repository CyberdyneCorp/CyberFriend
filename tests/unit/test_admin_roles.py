"""Console roles: one route table, deny by default, and tokens downscoped by the issuer.

The table is the boundary between read-only operators and the calls that purge
a person or widen what the agent reaches, so most of these tests walk every
route the router actually mounts rather than a list somebody remembered to
update.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from chatmemory.admin.access import WRITE_ACCESS, Access, permits, route_keys
from chatmemory.admin.auth import Operator, Principal, Role
from chatmemory.admin.server import ROUTE_ACCESS, build_app
from chatmemory.domain.identity import PersonRef
from tests.unit.test_admin_api import build_console
from tests.unit.test_admin_api_federation import ISSUES

QUESTIONS_ROUTE = ("GET", "/api/usage/people/{id}/questions")

REQUIRES_ADMIN = {"error": "requires admin"}


def _mounted(app: Starlette) -> set[tuple[str, str]]:
    return {key for route in app.routes for key in route_keys(route)}


async def _both_shapes(tmp_path: Path) -> list[Starlette]:
    """The app with the bundle mounted and with the placeholder, which differ at /."""
    console = await build_console()
    services, tokens = console.services, console.tokens
    return [build_app(services, tokens), build_app(services, tokens, tmp_path)]


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


# --- the table covers exactly what is mounted --------------------------


async def test_every_mounted_route_has_a_row(tmp_path: Path) -> None:
    for app in await _both_shapes(tmp_path):
        missing = _mounted(app) - ROUTE_ACCESS.keys()
        assert not missing, f"routes without a row in ROUTE_ACCESS: {sorted(missing)}"


async def test_every_row_names_a_mounted_route(tmp_path: Path) -> None:
    """A row for a route that no longer exists hides what the table really covers."""
    mounted: set[tuple[str, str]] = set()
    for app in await _both_shapes(tmp_path):
        mounted |= _mounted(app)
    assert set(ROUTE_ACCESS) - mounted == set()


def test_no_write_is_below_admin() -> None:
    below = {key: access for key, access in ROUTE_ACCESS.items() if key[0] != "GET"}
    assert all(access in WRITE_ACCESS for access in below.values()), below


def test_nothing_under_the_api_is_public_or_a_user_route() -> None:
    exposed = {
        key
        for key, access in ROUTE_ACCESS.items()
        if key[1].startswith("/api") and access in {Access.PUBLIC, Access.USER}
    }
    assert exposed == set()


def test_question_text_needs_an_admin_signed_in_as_a_person() -> None:
    # The row lands with the usage change; until then it must not exist at a
    # weaker level either.
    if QUESTIONS_ROUTE in ROUTE_ACCESS:
        assert ROUTE_ACCESS[QUESTIONS_ROUTE] is Access.ADMIN_OIDC


async def test_a_route_without_a_row_is_refused_before_its_handler_runs() -> None:
    console = await build_console()
    table = {k: v for k, v in ROUTE_ACCESS.items() if k != ("POST", "/api/optouts")}
    app = build_app(console.services, console.tokens, route_access=table)

    response = TestClient(app).post(
        "/api/optouts",
        json={"platform": "discord", "platform_user_id": 42},
        headers=console.auth(),
    )

    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}
    assert console.optouts.purged == []


# --- principals --------------------------------------------------------


def test_personal_content_needs_an_admin_signed_in_as_a_person() -> None:
    """A break-glass token is never enough for `admin_oidc`, even when admin."""
    token_admin = Principal("ana", "ana", frozenset({Role.ADMIN}), "token")
    person_admin = Principal("ana", "oidc:ana", frozenset({Role.ADMIN}), "oidc")
    person_operator = Principal("ben", "oidc:ben", frozenset({Role.OPERATOR}), "oidc")

    assert not permits(token_admin, Access.ADMIN_OIDC)
    assert permits(person_admin, Access.ADMIN_OIDC)
    assert not permits(person_operator, Access.ADMIN_OIDC)


async def test_an_admin_oidc_route_refuses_an_admin_token() -> None:
    console = await build_console()  # no issuer: the token is admin
    table = {**ROUTE_ACCESS, ("POST", "/api/optouts"): Access.ADMIN_OIDC}
    app = build_app(console.services, console.tokens, route_access=table)

    response = TestClient(app).post(
        "/api/optouts",
        json={"platform": "discord", "platform_user_id": 42},
        headers=console.auth(),
    )

    assert (response.status_code, response.json()) == (403, REQUIRES_ADMIN)
    assert console.optouts.purged == []


def test_admin_implies_operator() -> None:
    admin = Principal("ana", "ana", frozenset({Role.ADMIN}), "oidc")
    assert admin.has(Role.OPERATOR)
    operator = Principal("ben", "ben", frozenset({Role.OPERATOR}), "oidc")
    assert not operator.has(Role.ADMIN)


def test_a_token_is_admin_only_while_no_issuer_is_configured() -> None:
    before = Principal.for_token(Operator("ana"), oidc_configured=False)
    after = Principal.for_token(Operator("ana"), oidc_configured=True)
    assert before.has(Role.ADMIN)
    assert after.has(Role.OPERATOR)
    assert not after.has(Role.ADMIN)
    assert before.via == after.via == "token"


# --- an operator changes nothing ---------------------------------------


async def test_an_operator_cannot_opt_a_person_out() -> None:
    console = await build_console(oidc_configured=True)

    response = console.client.post(
        "/api/optouts",
        json={"platform": "discord", "platform_user_id": 42},
        headers=console.auth(),
    )

    assert response.status_code == 403
    assert response.json() == REQUIRES_ADMIN
    assert console.optouts.purged == []
    assert console.changes._entries == []  # noqa: SLF001


async def test_an_operator_cannot_allow_a_federated_tool() -> None:
    console = await build_console(offers=ISSUES, oidc_configured=True)
    before = console.client.get("/api/federation/allowlist", headers=console.auth()).json()

    response = console.client.post(
        "/api/federation/allowlist",
        json={"server": "issues", "tool": "create_issue", "read_only": True},
        headers=console.auth(),
    )

    assert response.status_code == 403
    assert response.json() == REQUIRES_ADMIN
    after = console.client.get("/api/federation/allowlist", headers=console.auth()).json()
    assert after == before
    assert console.changes._entries == []  # noqa: SLF001


async def test_the_same_token_is_admin_while_no_issuer_is_configured() -> None:
    console = await build_console()

    response = console.client.post(
        "/api/optouts",
        json={"platform": "discord", "platform_user_id": 42},
        headers=console.auth(),
    )

    assert response.status_code == 200
    assert console.optouts.purged == [PersonRef("discord", 42)]


async def test_with_an_issuer_a_token_is_refused_on_every_admin_route() -> None:
    console = await build_console(oidc_configured=True)
    admin_rows = [key for key, access in ROUTE_ACCESS.items() if access in WRITE_ACCESS]
    assert admin_rows

    for method, path in admin_rows:
        response = console.client.request(method, _concrete(path), headers=console.auth())
        assert (response.status_code, response.json()) == (403, REQUIRES_ADMIN), (
            method,
            path,
        )
    assert console.optouts.purged == []
    assert console.changes._entries == []  # noqa: SLF001


#: What an operator reads today. Fixed rather than derived from the table, so
#: a read raised to admin fails here instead of silently leaving the list --
#: once the issuer is set, that would lock every operator out of the screen.
OPERATOR_READS = {
    "/api/status",
    "/api/audit",
    "/api/settings",
    "/api/federation/servers",
    "/api/federation/allowlist",
    "/api/channels",
    "/api/optouts",
    "/api/tokens",
}


def test_the_operator_reads_are_exactly_the_screens_operators_use() -> None:
    reads = {
        path
        for (method, path), access in ROUTE_ACCESS.items()
        if method == "GET" and access is Access.OPERATOR and "{" not in path
    }
    assert reads == OPERATOR_READS


async def test_with_an_issuer_a_token_still_reads() -> None:
    console = await build_console(oidc_configured=True)

    for path in sorted(OPERATOR_READS):
        assert console.client.get(path, headers=console.auth()).status_code == 200, path
    assert console.client.head("/api/status", headers=console.auth()).status_code == 200


# --- the 401 does not change -------------------------------------------


@pytest.mark.parametrize("oidc_configured", [False, True])
async def test_the_refusal_without_a_credential_is_byte_identical_everywhere(
    oidc_configured: bool,
) -> None:
    """Role checks come after authentication, so they add no oracle to the 401."""
    console = await build_console(oidc_configured=oidc_configured)
    requests = [
        ("GET", "/api/status"),
        ("POST", "/api/optouts"),
        ("DELETE", "/api/tokens/7"),
        ("GET", "/api/messages"),
        ("PATCH", "/api/status"),
    ]

    refusals = set()
    for method, path in requests:
        response = console.client.request(
            method, path, headers={"Authorization": "Bearer cfa_unknown"}
        )
        refusals.add(
            (
                response.status_code,
                response.content,
                response.headers.get("www-authenticate"),
                response.headers.get("content-type"),
            )
        )

    assert refusals == {
        (401, b'{"error":"unauthorized"}', 'Bearer realm="chatmemory-admin"', "application/json")
    }


# --- outside /api, and the prefix itself --------------------------------


async def test_a_wrong_method_on_a_probe_is_refused_not_405() -> None:
    """No row, no access -- a public GET row does not open the route's other verbs."""
    console = await build_console()

    anonymous = console.client.post("/health")
    with_token = console.client.post("/health", headers=console.auth())

    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"] == 'Bearer realm="chatmemory-admin"'
    assert (with_token.status_code, with_token.json()) == (403, {"error": "forbidden"})


async def test_the_bundle_is_public_and_the_prefix_is_not(tmp_path: Path) -> None:
    """The Mount at "/" claims "/api" and "/apix" too; they stay authenticated."""
    (tmp_path / "index.html").write_text("<!doctype html><title>console</title>")
    console = await build_console()
    client = TestClient(build_app(console.services, console.tokens, tmp_path))

    assert client.get("/index.html").status_code == 200
    assert client.post("/index.html").status_code == 401
    for path in ("/api", "/apix", "/api/", "/api/messages"):
        assert client.get(path).status_code == 401, path
