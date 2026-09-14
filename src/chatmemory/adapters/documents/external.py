"""Read-only readers for the external systems we are configured to follow.

Three properties hold for every source in this module, and they are the
reason it exists rather than a general "fetch a URL" helper:

  * **Read-only by construction.** There is no write path here to call.
  * **Failure is indistinguishable.** Missing, forbidden, and refused all
    return None. Reporting which would confirm that a document exists, and
    confirming existence to whoever posted the link is the disclosure the
    bounded-credential rule is there to prevent.
  * **Membership is reported, not judged.** A fetch returns the containers
    (shared drive, folder, workspace) the document lives in, and the policy
    decides whether those are shared with the team. The adapter never decides
    that a document is in scope.

Credentials are supplied by the caller, on the httpx client: `drive_client` and
`notion_client` build one carrying the right headers. A source therefore never
holds more access than whoever constructed it chose to hand over.

The credential is the escalation path for this whole feature: an account that
can read more than the team can turns "post a link" into "make the bot read a
private document aloud". `ExternalPolicy.verify_credentials` refuses to start
with such a credential; nothing here can compensate if it is wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
import structlog

from chatmemory.app.documents.ports import ExternalDocumentSource, FetchedExternal

log = structlog.get_logger()

DRIVE_API = "https://www.googleapis.com/drive/v3/files"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

_DRIVE_ID = re.compile(r"/d/([A-Za-z0-9_-]{10,})|[?&]id=([A-Za-z0-9_-]{10,})")
_NOTION_ID = re.compile(r"([0-9a-fA-F]{32})|([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})")

# Google Docs are not files with bytes; they are exported. Plain text keeps the
# parser on the smallest, least attackable path.
DOCS_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}


@dataclass(frozen=True, slots=True)
class ExternalFetch:
    """One retrieved external document. Satisfies `FetchedExternal`."""

    data: bytes
    filename: str
    media_type: str
    revision: str | None = None
    containers: frozenset[str] = field(default_factory=frozenset)


async def _get_json(
    client: httpx.AsyncClient, url: str, params: dict[str, str], timeout: float
) -> dict[str, object] | None:
    try:
        response = await client.get(url, params=params, timeout=timeout)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        # 403 and 404 deliberately collapse together here.
        return None
    payload = response.json()
    return payload if isinstance(payload, dict) else None


async def _get_bounded(
    client: httpx.AsyncClient, url: str, params: dict[str, str], max_bytes: int, timeout: float
) -> bytes | None:
    """Download with the cap enforced against the bytes, not the header."""
    try:
        async with client.stream("GET", url, params=params, timeout=timeout) as response:
            if response.status_code != 200:
                return None
            buffer = bytearray()
            async for piece in response.aiter_bytes(64 * 1024):
                buffer.extend(piece)
                if len(buffer) > max_bytes:
                    return None
            return bytes(buffer)
    except httpx.HTTPError:
        return None


class GoogleDriveSource:
    """Drive, read-only, through a credential scoped to what the team shares."""

    name = "drive"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @staticmethod
    def file_id(url: str) -> str | None:
        match = _DRIVE_ID.search(url)
        if match is None:
            return None
        return match.group(1) or match.group(2)

    async def fetch(self, url: str, max_bytes: int, timeout: float) -> ExternalFetch | None:
        file_id = self.file_id(url)
        if file_id is None:
            return None

        meta = await _get_json(
            self._client,
            f"{DRIVE_API}/{file_id}",
            {
                "fields": "id,name,mimeType,parents,driveId,modifiedTime",
                "supportsAllDrives": "true",
            },
            timeout,
        )
        if meta is None:
            return None

        mime = str(meta.get("mimeType", "application/octet-stream"))
        export = DOCS_EXPORT.get(mime)
        if export is not None:
            data = await _get_bounded(
                self._client,
                f"{DRIVE_API}/{file_id}/export",
                {"mimeType": export},
                max_bytes,
                timeout,
            )
            mime = export
        else:
            data = await _get_bounded(
                self._client,
                f"{DRIVE_API}/{file_id}",
                {"alt": "media", "supportsAllDrives": "true"},
                max_bytes,
                timeout,
            )
        if data is None:
            return None

        parents = meta.get("parents")
        containers = {str(p) for p in parents} if isinstance(parents, list) else set()
        drive_id = meta.get("driveId")
        if isinstance(drive_id, str):
            containers.add(drive_id)
        return ExternalFetch(
            data=data,
            filename=str(meta.get("name") or file_id),
            media_type=mime,
            revision=str(meta.get("modifiedTime") or "") or None,
            containers=frozenset(containers),
        )


class NotionSource:
    """Notion pages, read-only, rendered to plain text before parsing."""

    name = "notion"

    def __init__(self, client: httpx.AsyncClient, max_blocks: int = 500) -> None:
        self._client = client
        self._max_blocks = max_blocks

    @staticmethod
    def page_id(url: str) -> str | None:
        match = _NOTION_ID.search(url)
        if match is None:
            return None
        return match.group(1) or match.group(2)

    async def fetch(self, url: str, max_bytes: int, timeout: float) -> ExternalFetch | None:
        page_id = self.page_id(url)
        if page_id is None:
            return None

        page = await _get_json(self._client, f"{NOTION_API}/pages/{page_id}", {}, timeout)
        if page is None:
            return None

        blocks = await _get_json(
            self._client,
            f"{NOTION_API}/blocks/{page_id}/children",
            {"page_size": str(min(self._max_blocks, 100))},
            timeout,
        )
        if blocks is None:
            return None

        text = _notion_text(blocks)
        data = text.encode("utf-8")[:max_bytes]
        return ExternalFetch(
            data=data,
            filename=f"{page_id}.md",
            media_type="text/markdown",
            revision=str(page.get("last_edited_time") or "") or None,
            containers=frozenset(_notion_containers(page)),
        )


def _notion_containers(page: dict[str, object]) -> set[str]:
    """Where the page lives, as Notion reports it.

    Whether that place is shared with the team is the policy's decision, not
    this function's.
    """
    parent = page.get("parent")
    containers: set[str] = set()
    if isinstance(parent, dict):
        for key in ("workspace_id", "database_id", "page_id"):
            value = parent.get(key)
            if isinstance(value, str):
                containers.add(value)
    return containers


def _notion_text(blocks: dict[str, object]) -> str:
    """Flatten a block list to markdown-ish lines.

    Headings become `#` so the prose chunker can use them as boundaries; it is
    the only structure Notion gives us for free.
    """
    results = blocks.get("results")
    if not isinstance(results, list):
        return ""
    lines: list[str] = []
    for block in results:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type", ""))
        body = block.get(kind)
        if not isinstance(body, dict):
            continue
        rich = body.get("rich_text")
        if not isinstance(rich, list):
            continue
        text = "".join(
            str(span.get("plain_text", "")) for span in rich if isinstance(span, dict)
        ).strip()
        if not text:
            continue
        heading = kind.startswith("heading_") and kind[-1].isdigit()
        lines.append(("#" * int(kind[-1]) + " " if heading else "") + text)
    return "\n\n".join(lines)


def drive_client(access_token: str, timeout: float = 15.0) -> httpx.AsyncClient:
    """A client carrying the Drive credential.

    The credential lives in the client rather than in the source, so a source
    cannot be constructed with more access than the caller chose to give it.
    """
    return httpx.AsyncClient(
        headers={"Authorization": f"Bearer {access_token}"}, timeout=timeout
    )


def notion_client(access_token: str, timeout: float = 15.0) -> httpx.AsyncClient:
    """A client carrying the Notion credential and the API version it expects."""
    return httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {access_token}",
            "Notion-Version": NOTION_VERSION,
        },
        timeout=timeout,
    )


def build_sources(
    names: frozenset[str], client: httpx.AsyncClient
) -> list[GoogleDriveSource | NotionSource]:
    """Instantiate the configured sources, ignoring names we do not implement.

    An unknown name is not an error at startup, but it fetches nothing: a
    source with no reader is a source we never follow links into.
    """
    built: list[GoogleDriveSource | NotionSource] = []
    for name in sorted(names):
        if name == GoogleDriveSource.name:
            built.append(GoogleDriveSource(client))
        elif name == NotionSource.name:
            built.append(NotionSource(client))
        else:
            log.warning("document.unknown_external_source", source=name)
    return built


if TYPE_CHECKING:  # pragma: no cover - type-checking only

    def _fetch_conforms(fetched: ExternalFetch) -> FetchedExternal:
        return fetched

    def _drive_conforms(source: GoogleDriveSource) -> ExternalDocumentSource:
        return source

    def _notion_conforms(source: NotionSource) -> ExternalDocumentSource:
        return source
