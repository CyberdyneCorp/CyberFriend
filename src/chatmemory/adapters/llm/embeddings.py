"""Embeddings over any OpenAI-compatible endpoint.

The base URL is configuration, so the same image runs against OpenAI, a
LiteLLM proxy, or a self-hosted gateway without a code change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import structlog
from openai import APIError, AsyncOpenAI, RateLimitError

log = structlog.get_logger()


class OpenAICompatibleEmbeddings:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dimensions: int,
        batch_size: int = 64,
        max_retries: int = 5,
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._max_retries = max_retries

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        out: list[Sequence[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            out.extend(await self._embed_batch(batch))
        return out

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        delay = 1.0
        for attempt in range(self._max_retries):
            try:
                response = await self._client.embeddings.create(
                    model=self._model, input=batch
                )
                vectors = [d.embedding for d in response.data]
                self._check_dimensions(vectors)
                return vectors
            except (RateLimitError, APIError) as exc:
                if attempt == self._max_retries - 1:
                    raise
                log.warning("embed.retry", attempt=attempt + 1, error=type(exc).__name__)
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    def _check_dimensions(self, vectors: list[list[float]]) -> None:
        """Fail loudly on a dimension mismatch.

        The schema pins the vector width, so a model returning a different
        size fails at insert with an opaque error far from the cause. Changing
        the embedding model is a reindex, not a swap.
        """
        for vector in vectors:
            if len(vector) != self._dimensions:
                raise ValueError(
                    f"embedding model {self._model!r} returned {len(vector)} dimensions, "
                    f"but the schema expects {self._dimensions}; "
                    "changing the model requires a reindex"
                )
