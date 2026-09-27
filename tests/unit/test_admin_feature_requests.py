"""The console's feature-request screen: operators read, admins triage, all recorded."""

from __future__ import annotations

from chatmemory.admin.audit import ChangeKind
from chatmemory.ports.feature_requests import RequestStatus
from tests.unit.test_admin_api import build_console, suggestion

REQUIRES_ADMIN = {"error": "requires admin"}


async def test_operators_read_the_list_newest_first() -> None:
    console = await build_console(
        oidc_configured=True, suggestions=[suggestion(1), suggestion(2, text="voice notes")]
    )

    response = console.client.get("/api/feature-requests", headers=console.auth())

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["items"]] == [2, 1]
    assert body["total"] == 2
    assert body["items"][0] == {
        "id": 2,
        "text": "voice notes",
        "language": "en",
        "status": "new",
        "admin_note": None,
        "duplicate_of": None,
        "source_kind": "command",
        "person": "Sam",
        "same_text_elsewhere": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "updated_by": None,
    }


async def test_the_list_filters_by_status() -> None:
    console = await build_console(
        suggestions=[suggestion(1), suggestion(2, RequestStatus.PLANNED)]
    )

    response = console.client.get(
        "/api/feature-requests?status=planned", headers=console.auth()
    )

    assert [item["id"] for item in response.json()["items"]] == [2]


async def test_an_unknown_status_filter_is_refused() -> None:
    console = await build_console()

    response = console.client.get("/api/feature-requests?status=shipped", headers=console.auth())

    assert response.status_code == 400
    assert "status must be one of" in response.json()["error"]


async def test_an_operator_cannot_triage() -> None:
    console = await build_console(oidc_configured=True, suggestions=[suggestion(1)])

    response = console.client.patch(
        "/api/feature-requests/1", json={"status": "planned"}, headers=console.auth()
    )

    assert (response.status_code, response.json()) == (403, REQUIRES_ADMIN)
    assert console.feature_requests.changes == []
    assert console.feature_requests.rows[1].status is RequestStatus.NEW
    assert console.changes._entries == []  # noqa: SLF001


async def test_an_admin_sets_the_status_and_the_change_is_recorded() -> None:
    console = await build_console(suggestions=[suggestion(7)])

    response = console.client.patch(
        "/api/feature-requests/7", json={"status": "planned"}, headers=console.auth()
    )

    assert response.status_code == 200
    assert response.json()["request"]["status"] == "planned"
    assert response.json()["request"]["updated_by"] == "ana"
    [entry] = await console.changes.recent()
    assert (entry.operator, entry.setting, entry.kind) == (
        "ana",
        "feature_request.7.status",
        ChangeKind.APPLIED,
    )
    assert (entry.before, entry.after) == ("new", "planned")


async def test_the_note_is_recorded_as_set_never_as_its_text() -> None:
    console = await build_console(suggestions=[suggestion(7)])

    console.client.patch(
        "/api/feature-requests/7",
        json={"admin_note": "talk to Bea about the API budget"},
        headers=console.auth(),
    )

    [entry] = await console.changes.recent()
    assert entry.setting == "feature_request.7.admin_note"
    assert (entry.before, entry.after) == ("empty", "set")
    assert console.feature_requests.rows[7].admin_note == "talk to Bea about the API budget"


async def test_marking_a_duplicate_records_both_fields() -> None:
    console = await build_console(suggestions=[suggestion(3), suggestion(7)])

    response = console.client.patch(
        "/api/feature-requests/7",
        json={"status": "duplicate", "duplicate_of": 3},
        headers=console.auth(),
    )

    assert response.status_code == 200
    settings = {e.setting: (e.before, e.after) for e in await console.changes.recent()}
    assert settings == {
        "feature_request.7.status": ("new", "duplicate"),
        "feature_request.7.duplicate_of": (None, "#3"),
    }


async def test_an_unchanged_value_records_nothing() -> None:
    console = await build_console(suggestions=[suggestion(7)])

    response = console.client.patch(
        "/api/feature-requests/7", json={"status": "new"}, headers=console.auth()
    )

    assert response.status_code == 200
    assert await console.changes.recent() == ()


async def test_the_text_cannot_be_changed() -> None:
    console = await build_console(suggestions=[suggestion(7)])

    response = console.client.patch(
        "/api/feature-requests/7", json={"text": "something else"}, headers=console.auth()
    )

    assert response.status_code == 400
    assert console.feature_requests.changes == []


async def test_bad_values_are_refused_before_anything_changes() -> None:
    console = await build_console(suggestions=[suggestion(7)])
    bad = [
        {},
        {"status": "shipped"},
        {"admin_note": 12},
        {"admin_note": "x" * 2001},
        {"duplicate_of": 7},
        {"duplicate_of": True},
        {"duplicate_of": "seven"},
    ]

    for body in bad:
        response = console.client.patch(
            "/api/feature-requests/7", json=body, headers=console.auth()
        )
        assert response.status_code == 400, body

    assert console.feature_requests.changes == []
    assert await console.changes.recent() == ()


async def test_an_unknown_duplicate_target_is_refused() -> None:
    console = await build_console(suggestions=[suggestion(7)])

    response = console.client.patch(
        "/api/feature-requests/7", json={"duplicate_of": 99}, headers=console.auth()
    )

    assert response.status_code == 400
    assert await console.changes.recent() == ()


async def test_an_unknown_suggestion_is_404() -> None:
    console = await build_console()

    response = console.client.patch(
        "/api/feature-requests/5", json={"status": "done"}, headers=console.auth()
    )

    assert response.status_code == 404
    assert await console.changes.recent() == ()
