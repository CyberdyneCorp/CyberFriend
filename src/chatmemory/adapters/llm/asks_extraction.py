"""Ask extraction over any OpenAI-compatible endpoint.

Same shape as `embeddings.py`, for the same reason: the base URL is
configuration, so the extractor runs against OpenAI, a LiteLLM proxy or a
self-hosted gateway without a code change.

Two things differ from the embedding client. Structured output is requested
with a strict schema and *verified* rather than trusted -- serving stacks vary
in how well they honour it, and a response that is merely JSON-shaped must not
become an obligation. And a failed extraction returns nothing rather than
raising: one unreadable message must not stop ingestion, and an ask that was
missed is recoverable in a way that a stalled pipeline is not.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import structlog
from openai import APIError, AsyncOpenAI, BadRequestError, RateLimitError
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared_params import ResponseFormatJSONObject, ResponseFormatJSONSchema

from chatmemory.app.asks.model import AskCandidate, ExtractedAsk
from chatmemory.app.asks.prompt import (
    OUTPUT_SCHEMA,
    RESPONSE_FORMAT,
    SYSTEM_PROMPT,
    parse_extractions,
    render_candidate,
)
from chatmemory.domain.identity import PersonRef

log = structlog.get_logger()


@dataclass
class UsageMeter:
    """Tokens spent, so the standing cost can be reported rather than guessed."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def record(self, prompt: int, completion: int) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion

    @property
    def average_prompt_tokens(self) -> float:
        return self.prompt_tokens / self.calls if self.calls else 0.0

    @property
    def average_completion_tokens(self) -> float:
        return self.completion_tokens / self.calls if self.calls else 0.0


@dataclass(frozen=True, slots=True)
class ExtractorConfig:
    api_key: str
    base_url: str
    # The cheap model: extraction runs over traffic, not over questions, so a
    # frontier model here is a standing bill nobody asked for.
    model: str = "gpt-4o-mini"
    max_retries: int = 3
    # Deterministic by default. Two runs over the same message disagreeing
    # about whether somebody owes something is worse than either answer.
    temperature: float = 0.0
    timeout_seconds: float = 30.0
    names: Mapping[PersonRef, str] = field(default_factory=dict)


class OpenAICompatibleAskExtractor:
    """Implements `AskExtractor`."""

    def __init__(self, config: ExtractorConfig, usage: UsageMeter | None = None) -> None:
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )
        self._config = config
        self.usage = usage or UsageMeter()

    async def extract(self, candidate: AskCandidate) -> Sequence[ExtractedAsk]:
        payload = await self._call(render_candidate(candidate, self._config.names))
        if payload is None:
            return []
        return parse_extractions(payload)

    async def _call(self, rendered: str) -> Mapping[str, Any] | None:
        delay = 1.0
        response_format: dict[str, Any] = RESPONSE_FORMAT
        for attempt in range(self._config.max_retries):
            try:
                return await self._once(rendered, response_format)
            except BadRequestError:
                if response_format is RESPONSE_FORMAT:
                    # Some serving stacks accept `json_object` but not a named
                    # schema. Falling back keeps the schema in the prompt, so
                    # the output is still checked -- just not by the server.
                    log.warning("asks.schema_unsupported", model=self._config.model)
                    response_format = {"type": "json_object"}
                    continue
                raise
            except (RateLimitError, APIError) as exc:
                if attempt == self._config.max_retries - 1:
                    raise
                log.warning(
                    "asks.extract_retry", attempt=attempt + 1, error=type(exc).__name__
                )
                await asyncio.sleep(delay)
                delay *= 2
        return None

    async def _once(
        self, rendered: str, response_format: dict[str, Any]
    ) -> Mapping[str, Any] | None:
        system = SYSTEM_PROMPT
        if response_format.get("type") == "json_object":
            system = (
                f"{SYSTEM_PROMPT}\nReturn JSON matching this schema:\n"
                f"{json.dumps(OUTPUT_SCHEMA)}"
            )

        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            # The candidate goes in as the *user* turn and nowhere else:
            # message content is data to extract from, never instruction, and
            # splicing it into the system prompt is how it would become the
            # latter.
            {"role": "user", "content": rendered},
        ]
        response = await self._client.chat.completions.create(
            model=self._config.model,
            temperature=self._config.temperature,
            # Cast at the boundary rather than widening the SDK's parameter
            # types. The schema is built as plain data in the app layer, which
            # must not import the SDK to describe it.
            response_format=cast(
                "ResponseFormatJSONSchema | ResponseFormatJSONObject", response_format
            ),
            messages=messages,
        )
        usage = response.usage
        if usage is not None:
            self.usage.record(usage.prompt_tokens, usage.completion_tokens)

        content = response.choices[0].message.content if response.choices else None
        if not content:
            return None
        return _as_mapping(content)


def _as_mapping(content: str) -> Mapping[str, Any] | None:
    """Parse a response body, tolerating the ways stacks wrap JSON.

    A model that returns prose instead of an object has failed to extract, not
    produced an ask: anything unparseable is dropped, never salvaged by regex
    into an obligation somebody would then be told they owe.
    """
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        log.warning("asks.unparseable_response")
        return None
    if isinstance(parsed, list):
        # A stack that dropped the wrapper object and returned the array.
        return {"asks": parsed}
    if isinstance(parsed, dict):
        return parsed
    return None
