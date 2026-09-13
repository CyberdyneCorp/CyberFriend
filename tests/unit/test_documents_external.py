"""The external readers, against a stubbed transport.

Two behaviours are asserted rather than the API shapes: a fetch reports the
containers a document lives in so the policy can judge scope, and every kind
of failure looks identical from outside -- a 404 and a 403 must be the same
answer, because the difference between them is the fact that the document
exists.
"""

from __future__ import annotations

import httpx

from chatmemory.adapters.documents.external import (
    NOTION_VERSION,
    GoogleDriveSource,
    NotionSource,
    build_sources,
    drive_client,
    notion_client,
)
from chatmemory.adapters.documents.http import BoundedHttpFetcher

DRIVE_FILE = "https://drive.google.com/file/d/FILE1234567/view"


def client_for(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


# --- Drive --------------------------------------------------------------


def drive_handler(
    status: int = 200, containers: list[str] | None = None, body: bytes = b"# Spec\n\nText."
) -> object:
    def handle(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status)
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=body)
        return httpx.Response(
            200,
            json={
                "id": "FILE1234567",
                "name": "spec.md",
                "mimeType": "text/markdown",
                "parents": containers if containers is not None else ["team-drive"],
                "modifiedTime": "2026-09-01T00:00:00Z",
            },
        )

    return handle


async def test_a_drive_document_comes_back_with_its_containers() -> None:
    async with client_for(drive_handler()) as client:
        fetched = await GoogleDriveSource(client).fetch(DRIVE_FILE, 1_000_000, 5.0)

    assert fetched is not None
    assert fetched.filename == "spec.md"
    assert fetched.containers == frozenset({"team-drive"})
    assert fetched.revision == "2026-09-01T00:00:00Z"


async def test_forbidden_and_missing_are_the_same_answer() -> None:
    """Neither may confirm that the document exists."""
    async with client_for(drive_handler(status=403)) as client:
        forbidden = await GoogleDriveSource(client).fetch(DRIVE_FILE, 1_000_000, 5.0)
    async with client_for(drive_handler(status=404)) as client:
        missing = await GoogleDriveSource(client).fetch(DRIVE_FILE, 1_000_000, 5.0)

    assert forbidden is None
    assert missing is None


async def test_a_url_with_no_document_id_fetches_nothing() -> None:
    async with client_for(drive_handler()) as client:
        assert await GoogleDriveSource(client).fetch("https://drive.google.com/", 1, 5.0) is None


async def test_a_document_larger_than_the_limit_is_abandoned() -> None:
    async with client_for(drive_handler(body=b"x" * 5000)) as client:
        assert await GoogleDriveSource(client).fetch(DRIVE_FILE, 100, 5.0) is None


async def test_a_transport_error_is_not_raised_at_the_caller() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with client_for(explode) as client:
        assert await GoogleDriveSource(client).fetch(DRIVE_FILE, 1_000_000, 5.0) is None


# --- Notion -------------------------------------------------------------

NOTION_PAGE = "https://www.notion.so/team/Spec-0123456789abcdef0123456789abcdef"


async def test_a_notion_page_becomes_markdown_with_its_workspace_as_container() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if "/blocks/" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "type": "heading_1",
                            "heading_1": {"rich_text": [{"plain_text": "Rollout"}]},
                        },
                        {
                            "type": "paragraph",
                            "paragraph": {"rich_text": [{"plain_text": "Thursday."}]},
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "parent": {"workspace_id": "team-workspace"},
                "last_edited_time": "2026-09-02T00:00:00Z",
            },
        )

    async with client_for(handle) as client:
        fetched = await NotionSource(client).fetch(NOTION_PAGE, 1_000_000, 5.0)

    assert fetched is not None
    assert fetched.data.decode().startswith("# Rollout")
    assert fetched.containers == frozenset({"team-workspace"})


def test_only_implemented_sources_are_built() -> None:
    async def unused(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200)

    client = httpx.AsyncClient()
    built = build_sources(frozenset({"drive", "notion", "sharepoint"}), client)
    assert sorted(source.name for source in built) == ["drive", "notion"]


def test_the_credential_lives_on_the_client_not_in_the_source() -> None:
    """A source cannot hold more access than whoever built it handed over."""
    assert dict(drive_client("token").headers)["authorization"] == "Bearer token"
    notion = dict(notion_client("token").headers)
    assert notion["notion-version"] == NOTION_VERSION


# --- attachment fetching ------------------------------------------------


async def test_attachment_bytes_are_capped_against_what_arrives() -> None:
    """The cap is enforced on the stream, not on a Content-Length claim."""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"y" * 10_000)

    async with client_for(handle) as client:
        fetcher = BoundedHttpFetcher(client)
        assert await fetcher.fetch("https://cdn.test/f.md", 10_000, 5.0) is not None
        assert await fetcher.fetch("https://cdn.test/f.md", 100, 5.0) is None


async def test_a_non_http_url_is_never_opened() -> None:
    fetcher = BoundedHttpFetcher()
    assert await fetcher.fetch("file:///etc/passwd", 1000, 1.0) is None
