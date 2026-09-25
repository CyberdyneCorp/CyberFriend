"""Admin authentication: the credential names the operator, and nothing else does.

These are the tests that matter if the console is reachable from anywhere but
localhost. Most of them assert a negative -- that no header, parameter or body
field offers a way to choose whose name a change is recorded under, and that a
refusal tells you nothing about why it refused.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

import chatmemory
from chatmemory.admin import auth as auth_module
from chatmemory.admin.access import Access, RouteAccess
from chatmemory.admin.auth import (
    AdminAuthenticator,
    AdminAuthMiddleware,
    InMemoryOperatorTokens,
    Operator,
    Unauthenticated,
    bearer_token,
    current_operator,
    generate_admin_token,
)
from chatmemory.app.tokens import hash_token

ANA = Operator("ana")
BEN = Operator("ben")


def build_console(tokens: InMemoryOperatorTokens) -> Starlette:
    """A console-shaped app: one protected route that reports who is acting."""

    async def whoami(request: Request) -> JSONResponse:
        # Reads the context, never the request. There is no other way to
        # learn who is acting, which is the property under test.
        return JSONResponse({"operator": current_operator().name})

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/api/whoami", whoami, methods=["GET", "POST"]),
            Route("/health", health),
        ]
    )
    table = {
        ("GET", "/api/whoami"): Access.OPERATOR,
        ("POST", "/api/whoami"): Access.ADMIN,
        ("GET", "/health"): Access.PUBLIC,
    }
    app.add_middleware(
        AdminAuthMiddleware,
        authenticator=AdminAuthenticator(tokens),
        access=RouteAccess(table, app.router.routes),
    )
    return app


# --- operator names ----------------------------------------------------


@pytest.mark.parametrize("name", ["ana", "ben.smith", "ops-2", "a", "x_9"])
def test_a_reasonable_operator_name_is_accepted(name: str) -> None:
    assert Operator(name).name == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "Ana",  # case is normalised by the operator, not silently by us
        "ana smith",
        "ana\n",
        "-leading",
        "a" * 65,
    ],
)
def test_an_unusable_operator_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError):
        Operator(name)


def test_an_operator_name_cannot_forge_the_shell_actor() -> None:
    """':' is what separates a person from the CLI in the change record."""
    from chatmemory.admin.audit import SHELL_ACTOR

    assert ":" in SHELL_ACTOR
    with pytest.raises(ValueError):
        Operator(SHELL_ACTOR)


# --- header parsing ----------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic cfa_x", "cfa_x", "bearer", "Token cfa_x"],
)
def test_a_credential_that_is_not_a_bearer_token_yields_nothing(
    header: str | None,
) -> None:
    assert bearer_token(header) is None


def test_the_scheme_is_matched_case_insensitively() -> None:
    assert bearer_token("bEaReR cfa_abc") == "cfa_abc"


# --- the credential decides --------------------------------------------


async def test_a_valid_credential_acts_as_the_operator_it_names() -> None:
    tokens = InMemoryOperatorTokens()
    issued = await tokens.issue(ANA, "laptop")
    with TestClient(build_console(tokens)) as client:
        response = client.get(
            "/api/whoami", headers={"Authorization": f"Bearer {issued.token}"}
        )
    assert response.status_code == 200
    assert response.json() == {"operator": "ana"}


async def test_two_operators_are_told_apart_by_their_credentials() -> None:
    tokens = InMemoryOperatorTokens()
    for operator in (ANA, BEN):
        issued = await tokens.issue(operator)
        with TestClient(build_console(tokens)) as client:
            response = client.get(
                "/api/whoami", headers={"Authorization": f"Bearer {issued.token}"}
            )
        assert response.json() == {"operator": operator.name}


async def test_health_stays_open() -> None:
    """A probe holds no credential and the endpoint exposes no configuration."""
    tokens = InMemoryOperatorTokens()
    with TestClient(build_console(tokens)) as client:
        assert client.get("/health").status_code == 200


# --- the guard and the router must match the same path -----------------
#
# The console is documented as deployable under a prefix -- /console/ behind a
# proxy, or mounted inside another ASGI app. Both shapes set `root_path`, and
# the router resolves handlers on the path with `root_path` stripped. A guard
# that read the raw path would stop matching exactly where the router keeps
# matching, so every /api route would answer with no operator bound. These
# tests fix the two paths together: whatever the router dispatches, the
# middleware must have authenticated.


async def test_a_console_behind_a_root_path_still_refuses_an_anonymous_call() -> None:
    tokens = InMemoryOperatorTokens()
    with TestClient(build_console(tokens), root_path="/console") as client:
        response = client.get("/console/api/whoami")
    assert response.status_code == 401


async def test_a_console_mounted_under_a_prefix_still_refuses_an_anonymous_call() -> None:
    tokens = InMemoryOperatorTokens()
    outer = Starlette(routes=[Mount("/console", app=build_console(tokens))])
    with TestClient(outer) as client:
        response = client.get("/console/api/whoami")
    assert response.status_code == 401


async def test_a_credential_still_names_its_operator_behind_a_root_path() -> None:
    """The guard must not simply refuse everything behind a prefix either."""
    tokens = InMemoryOperatorTokens()
    issued = await tokens.issue(ANA, "laptop")
    with TestClient(build_console(tokens), root_path="/console") as client:
        response = client.get(
            "/console/api/whoami", headers={"Authorization": f"Bearer {issued.token}"}
        )
    assert response.status_code == 200
    assert response.json() == {"operator": "ana"}


async def test_health_stays_open_behind_a_root_path() -> None:
    tokens = InMemoryOperatorTokens()
    with TestClient(build_console(tokens), root_path="/console") as client:
        assert client.get("/console/health").status_code == 200


# --- one refusal, four causes ------------------------------------------


async def _refusal(
    tokens: InMemoryOperatorTokens, headers: list[tuple[str, str]]
) -> tuple[int, bytes, str, str]:
    with TestClient(build_console(tokens)) as client:
        response = client.get("/api/whoami", headers=headers)
    return (
        response.status_code,
        response.content,
        response.headers.get("www-authenticate", ""),
        response.headers.get("content-type", ""),
    )


async def test_missing_malformed_unknown_and_revoked_are_indistinguishable() -> None:
    """A refusal that says which one you got enumerates who holds a credential."""
    tokens = InMemoryOperatorTokens()
    unknown = generate_admin_token()
    revoked = await tokens.issue(ANA)
    await tokens.revoke(ANA)

    refusals = {
        "missing": await _refusal(tokens, []),
        "malformed": await _refusal(tokens, [("Authorization", "Basic abc")]),
        "empty-bearer": await _refusal(tokens, [("Authorization", "Bearer ")]),
        "unknown": await _refusal(tokens, [("Authorization", f"Bearer {unknown}")]),
        "revoked": await _refusal(
            tokens, [("Authorization", f"Bearer {revoked.token}")]
        ),
        "two-credentials": await _refusal(
            tokens,
            [
                ("Authorization", f"Bearer {revoked.token}"),
                ("Authorization", f"Bearer {unknown}"),
            ],
        ),
    }
    distinct = set(refusals.values())
    assert len(distinct) == 1, f"refusals differ between cases: {refusals}"
    assert next(iter(distinct))[0] == 401


async def test_two_credentials_on_one_request_are_refused_not_resolved() -> None:
    """A live credential alongside another one must not authenticate.

    Reading the first or the last is a choice, and a proxy in front may make
    the opposite one -- which would attribute a change to the wrong person on
    the one surface where attribution is the product.
    """
    tokens = InMemoryOperatorTokens()
    live = await tokens.issue(ANA)
    other = await tokens.issue(BEN)
    with TestClient(build_console(tokens)) as client:
        response = client.get(
            "/api/whoami",
            headers=[
                ("Authorization", f"Bearer {live.token}"),
                ("Authorization", f"Bearer {other.token}"),
            ],
        )
    assert response.status_code == 401


# --- a request cannot name the operator it acts as ---------------------


async def test_naming_an_operator_in_the_request_has_no_effect() -> None:
    tokens = InMemoryOperatorTokens()
    issued = await tokens.issue(ANA)
    with TestClient(build_console(tokens)) as client:
        response = client.post(
            "/api/whoami?operator=ben&as=ben",
            headers={
                "Authorization": f"Bearer {issued.token}",
                "X-Operator": "ben",
                "X-Admin-Operator": "ben",
                "X-Forwarded-User": "ben",
            },
            json={"operator": "ben", "acting_as": "ben"},
        )
    assert response.json() == {"operator": "ana"}


async def test_naming_an_operator_without_a_credential_still_refuses() -> None:
    tokens = InMemoryOperatorTokens()
    await tokens.issue(ANA)
    with TestClient(build_console(tokens)) as client:
        response = client.post(
            "/api/whoami?operator=ana",
            headers={"X-Operator": "ana"},
            json={"operator": "ana"},
        )
    assert response.status_code == 401


def test_the_middleware_reads_no_header_but_the_credentials_and_origin() -> None:
    """Structural, because the behavioural tests can only cover names we guess.

    A header this file never thought of is exactly how a caller-supplied
    identity gets read back in, so the check is on what the module can read
    at all rather than on a list of forbidden names. Identity comes from
    `authorization` or the session `cookie`; `origin` (and the CSRF header)
    can only refuse a request, never name anyone.
    """
    source = (Path(chatmemory.__file__).parent / "admin" / "auth.py").read_text()
    tree = ast.parse(source)
    byte_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, bytes)
    }
    header_names = {b for b in byte_literals if b.islower() and b.isalpha()}
    assert header_names == {b"authorization", b"cookie", b"origin"}, (
        f"the middleware compares header names it should not: {header_names}"
    )
    # The body is never read for identity: `receive` is passed straight
    # through, and the query string is not consulted at all.
    assert "query_string" not in source
    assert "await receive" not in source


def test_the_authenticator_offers_no_way_to_ask_for_an_operator_by_name() -> None:
    """Impersonation needs an entry point. There must not be one."""
    methods = {
        name
        for name, value in vars(AdminAuthenticator).items()
        if not name.startswith("__") and callable(value)
    }
    assert methods == {"principal_for_token"}


# --- revocation is per operator ----------------------------------------


async def test_revoking_one_operator_leaves_every_other_credential_working() -> None:
    tokens = InMemoryOperatorTokens()
    ana = await tokens.issue(ANA, "laptop")
    ben = await tokens.issue(BEN, "laptop")
    ben_desktop = await tokens.issue(BEN, "desktop")

    assert await tokens.revoke(ANA) == 1

    assert await tokens.operator_for_token(ana.token) is None
    assert await tokens.operator_for_token(ben.token) == BEN
    assert await tokens.operator_for_token(ben_desktop.token) == BEN


async def test_revoking_an_operator_withdraws_all_of_their_credentials() -> None:
    tokens = InMemoryOperatorTokens()
    first = await tokens.issue(BEN, "laptop")
    second = await tokens.issue(BEN, "desktop")
    assert await tokens.revoke(BEN) == 2
    assert await tokens.operator_for_token(first.token) is None
    assert await tokens.operator_for_token(second.token) is None
    assert await tokens.live_credentials(BEN) == 0


async def test_revoking_an_operator_who_holds_nothing_is_not_an_error() -> None:
    assert await InMemoryOperatorTokens().revoke(ANA) == 0


# --- the credential is never stored --------------------------------------


async def test_only_the_hash_is_stored() -> None:
    tokens = InMemoryOperatorTokens()
    issued = await tokens.issue(ANA, "laptop")
    stored = await tokens.active_tokens()
    assert [r.token_hash for r in stored] == [hash_token(issued.token)]
    assert issued.token not in repr(stored)


def test_a_console_credential_is_distinguishable_from_an_mcp_one() -> None:
    """A token pasted into the wrong surface, or found in a log, is identifiable."""
    from chatmemory.app.tokens import TOKEN_PREFIX

    assert generate_admin_token().startswith(auth_module.ADMIN_TOKEN_PREFIX)
    assert auth_module.ADMIN_TOKEN_PREFIX != TOKEN_PREFIX


def test_two_credentials_are_never_the_same() -> None:
    assert len({generate_admin_token() for _ in range(64)}) == 64


# --- no operator in scope fails closed ---------------------------------


def test_current_operator_refuses_rather_than_defaulting() -> None:
    with pytest.raises(Unauthenticated):
        current_operator()
