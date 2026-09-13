"""The served application: what is exposed, and what is open.

Small surface, deliberately. Every route that can reach message content must
sit behind the credential; every route an orchestrator probes must not.
"""

from __future__ import annotations

from collections.abc import Sequence

from starlette.testclient import TestClient

from chatmemory.app.tokens import InMemoryTokenStore
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.messages import Message
from chatmemory.domain.search import SearchHit, SearchQuery
from chatmemory.health import HealthState
from chatmemory.mcp.auth import Authenticator
from chatmemory.mcp.server import build_app

GUILD = 7000


class EmptySearch:
    async def search(self, viewer: Viewer, query: SearchQuery) -> Sequence[SearchHit]:
        return []

    async def thread_context(
        self, viewer: Viewer, platform_message_id: int, radius: int = 10
    ) -> Sequence[Message]:
        return []

    async def list_channels(self, viewer: Viewer) -> Sequence[ChannelRef]:
        return []


class DenyAll:
    async def resolve_viewer(self, person: PersonRef) -> Viewer:
        return Viewer(person=person, visible_channels=frozenset())


def build(
    state: HealthState | None = None, ready: bool = True
) -> TestClient:
    async def readiness() -> bool:
        return ready

    app = build_app(
        search=EmptySearch(),
        authenticator=Authenticator(InMemoryTokenStore(), DenyAll()),
        guild_id=GUILD,
        state=state,
        readiness=readiness,
    )
    # Not entered as a context manager: these cases are answered before the
    # streamable-HTTP session manager is involved.
    return TestClient(app)


def test_the_application_exposes_only_three_routes() -> None:
    """A fourth route is a fourth thing to get the credential check right on."""
    app = build().app
    paths = {getattr(route, "path", None) for route in app.routes}  # type: ignore[union-attr]
    assert paths == {"/mcp", "/health", "/ready"}


def test_the_tool_endpoint_is_refused_without_a_credential() -> None:
    response = build().post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_health_is_open_and_dependency_free() -> None:
    """A database blip must not restart-loop the service."""
    response = build().get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_reports_ready() -> None:
    response = build(ready=True).get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_readiness_reports_503_when_a_dependency_is_missing() -> None:
    """503, so an orchestrator stops sending traffic to a server that would
    answer every question with silence."""
    response = build(ready=False).get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_health_surfaces_the_silent_failure_signals() -> None:
    state = HealthState(gateway_connected=True, backfill_lag_seconds=9.0, embedding_backlog=17)
    body = build(state=state).get("/health").json()
    assert body["gateway_connected"] is True
    assert body["backfill_lag_seconds"] == 9.0
    assert body["embedding_backlog"] == 17
