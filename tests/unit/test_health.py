from __future__ import annotations

from starlette.testclient import TestClient

from chatmemory.health import HealthState, build_app


def test_health_is_ok_even_when_gateway_is_down() -> None:
    """Liveness must not depend on dependencies, or a blip restart-loops us."""
    client = TestClient(build_app(HealthState(gateway_connected=False)))
    assert client.get("/health").status_code == 200


def test_ready_reports_503_when_gateway_disconnected() -> None:
    client = TestClient(build_app(HealthState(gateway_connected=False)))
    r = client.get("/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"


def test_ready_reports_200_when_connected() -> None:
    client = TestClient(build_app(HealthState(gateway_connected=True)))
    assert client.get("/ready").status_code == 200


def test_health_exposes_silent_failure_signals() -> None:
    state = HealthState(gateway_connected=True, backfill_lag_seconds=12.5, embedding_backlog=40)
    body = TestClient(build_app(state)).get("/health").json()
    assert body["backfill_lag_seconds"] == 12.5
    assert body["embedding_backlog"] == 40
