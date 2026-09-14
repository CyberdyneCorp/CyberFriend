"""Bounded retrieval of attachment bytes.

Attachments are fetched when the message is ingested rather than by keeping
the link: Discord's CDN URLs expire, and a corpus whose evidence stops
resolving after a few weeks is a corpus that cannot answer questions about
last quarter.

Every limit here is enforced against the bytes as they arrive rather than
against a Content-Length header, because the header is a claim. A stream that
keeps producing is abandoned at the cap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import structlog

from chatmemory.app.documents.ports import ContentFetcher

log = structlog.get_logger()

# Redirects are followed, but not indefinitely, and never off http(s).
MAX_REDIRECTS = 3
CHUNK = 64 * 1024


class BoundedHttpFetcher:
    """A `ContentFetcher` that gives up rather than reading without limit."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def fetch(self, url: str, max_bytes: int, timeout: float) -> bytes | None:
        """Bytes, or None. A failed attachment never fails its message."""
        if not url.lower().startswith(("http://", "https://")):
            return None
        client = self._client
        if client is None:
            async with self._new_client(timeout) as owned:
                return await self._stream(owned, url, max_bytes, timeout)
        return await self._stream(client, url, max_bytes, timeout)

    def _new_client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, max_redirects=MAX_REDIRECTS
        )

    async def _stream(
        self, client: httpx.AsyncClient, url: str, max_bytes: int, timeout: float
    ) -> bytes | None:
        try:
            async with client.stream("GET", url, timeout=timeout) as response:
                if response.status_code >= 400:
                    return None
                buffer = bytearray()
                async for piece in response.aiter_bytes(CHUNK):
                    buffer.extend(piece)
                    if len(buffer) > max_bytes:
                        # Abandon mid-stream: a server that keeps sending must
                        # not be able to decide how much memory we spend.
                        log.info("document.fetch_too_large", url=url)
                        return None
                return bytes(buffer)
        except (httpx.HTTPError, ValueError):
            log.info("document.fetch_failed", url=url)
            return None


if TYPE_CHECKING:  # pragma: no cover - type-checking only

    def _fetcher_conforms(fetcher: BoundedHttpFetcher) -> ContentFetcher:
        return fetcher
