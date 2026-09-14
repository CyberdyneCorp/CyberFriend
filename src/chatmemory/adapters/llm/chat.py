"""Chat completion over any OpenAI-compatible endpoint.

Same shape as the embeddings adapter: the base URL is configuration, so the
image runs against OpenAI, a LiteLLM proxy or a self-hosted gateway without a
code change.

What is different here is that the *capabilities* of whatever sits behind
that URL matter. Schema-constrained output and tool calling vary materially
between serving stacks -- vLLM, llama.cpp and Ollama do not behave the same,
and none of them behaves quite like OpenAI. So the model's capabilities are
declared, checked against what the enabled stages need at construction, and
the process refuses to start when one is missing, naming it. An unknown model
is assumed to do chat and nothing else: fail closed, because the alternative
is malformed JSON on request four hundred rather than a message at boot.

`probe()` is the same check against the live endpoint, for a composition root
that would rather find out at startup than on the first real question.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from typing import cast

import structlog
from openai import APIError, AsyncOpenAI, RateLimitError
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from openai.types.shared_params import ResponseFormatJSONSchema

from chatmemory.app.reasoning.capabilities import (
    MissingCapabilityError,
    ModelCapability,
    Stage,
    required_capabilities,
    validate_model_capabilities,
)
from chatmemory.app.reasoning.ports import JsonCompletion, TextCompletion

log = structlog.get_logger()

CHAT_ONLY = frozenset({ModelCapability.CHAT})
FULL = frozenset(
    {ModelCapability.CHAT, ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOL_CALLING}
)

KNOWN_MODEL_CAPABILITIES: Mapping[str, frozenset[ModelCapability]] = {
    "gpt-4o": FULL,
    "gpt-4o-mini": FULL,
    "gpt-4.1": FULL,
    "gpt-4.1-mini": FULL,
}
"""A hint, not an authority.

It covers endpoints whose behaviour is known; anything else must be declared
explicitly by the deployment, because a self-hosted stack serving a model of
the same name may not support the same things.
"""


def capabilities_for(model: str) -> frozenset[ModelCapability]:
    """What a model is assumed to provide when nothing was declared."""
    return KNOWN_MODEL_CAPABILITIES.get(model, CHAT_ONLY)


class OpenAICompatibleChat:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        stages: Iterable[Stage],
        provides: Iterable[ModelCapability] | None = None,
        scoring_model: str | None = None,
        temperature: float = 0.0,
        max_retries: int = 5,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._provides = frozenset(provides) if provides is not None else capabilities_for(model)
        self._stages = tuple(stages)
        # Boot fails here, naming the capability, rather than mid-request.
        validate_model_capabilities(model, self._provides, self._stages)
        self._client = client or AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._scoring_model = scoring_model or model
        self._temperature = temperature
        self._max_retries = max_retries

    @property
    def model(self) -> str:
        return self._model

    @property
    def provides(self) -> frozenset[ModelCapability]:
        return self._provides

    def scorer(self, stages: Iterable[Stage] = (Stage.SCORE,)) -> OpenAICompatibleChat:
        """A cheaper handle on the same provider, sharing this client.

        Per-candidate scoring fires once per surviving candidate and needs
        none of the deliberation the main judge does.
        """
        return OpenAICompatibleChat(
            api_key=self._api_key,
            base_url=self._base_url,
            model=self._scoring_model,
            stages=stages,
            provides=self._provides,
            temperature=self._temperature,
            max_retries=self._max_retries,
            client=self._client,
        )

    async def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], schema_name: str
    ) -> JsonCompletion:
        response = await self._create(
            system,
            user,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": dict(schema),
                    "strict": True,
                },
            },
        )
        content = self._content(response)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise MissingCapabilityError(
                f"model {self._model!r} returned output that is not JSON for schema "
                f"{schema_name!r}; the endpoint does not honour "
                f"{ModelCapability.STRUCTURED_OUTPUT}"
            ) from exc
        if not isinstance(parsed, dict):
            raise MissingCapabilityError(
                f"model {self._model!r} returned {type(parsed).__name__} rather than an "
                f"object for schema {schema_name!r}"
            )
        prompt_tokens, completion_tokens = self._usage(response)
        return JsonCompletion(
            data=parsed, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )

    async def complete_text(self, system: str, user: str) -> TextCompletion:
        response = await self._create(system, user)
        prompt_tokens, completion_tokens = self._usage(response)
        return TextCompletion(
            text=self._content(response),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def probe(self) -> None:
        """Verify the live endpoint really does what was declared.

        Runs only the capabilities the enabled stages need, and fails with
        the same error type as the declarative check so a composition root
        handles one thing.
        """
        needed = required_capabilities(self._stages)
        if ModelCapability.STRUCTURED_OUTPUT in needed:
            result = await self.complete_json(
                "Reply in the given schema.",
                "Return ok as true.",
                {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                "capability_probe",
            )
            if "ok" not in result.data:
                raise MissingCapabilityError(
                    f"model {self._model!r} ignored the response schema; "
                    f"{ModelCapability.STRUCTURED_OUTPUT} is not available"
                )
        if ModelCapability.TOOL_CALLING in needed:
            await self._probe_tool_calling()

    async def _probe_tool_calling(self) -> None:
        probe_messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": "Call the ping tool."}
        ]
        response = await self._client.chat.completions.create(
            model=self._model,
            temperature=self._temperature,
            messages=probe_messages,
            tools=[
                cast(
                    ChatCompletionToolParam,
                    {
                        "type": "function",
                        "function": {
                            "name": "ping",
                            "description": "Answer a capability probe.",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                )
            ],
        )
        if not response.choices or not response.choices[0].message.tool_calls:
            raise MissingCapabilityError(
                f"model {self._model!r} did not emit a tool call; "
                f"{ModelCapability.TOOL_CALLING} is not available"
            )

    async def _create(
        self, system: str, user: str, response_format: ResponseFormatJSONSchema | None = None
    ) -> ChatCompletion:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        delay = 1.0
        for attempt in range(self._max_retries):
            try:
                if response_format is None:
                    return await self._client.chat.completions.create(
                        model=self._model, temperature=self._temperature, messages=messages
                    )
                return await self._client.chat.completions.create(
                    model=self._model,
                    temperature=self._temperature,
                    messages=messages,
                    response_format=response_format,
                )
            except (RateLimitError, APIError) as exc:
                if attempt == self._max_retries - 1:
                    raise
                log.warning("chat.retry", attempt=attempt + 1, error=type(exc).__name__)
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    def _content(self, response: ChatCompletion) -> str:
        if not response.choices:
            raise MissingCapabilityError(f"model {self._model!r} returned no choices")
        return response.choices[0].message.content or ""

    def _usage(self, response: ChatCompletion) -> tuple[int, int]:
        usage = response.usage
        if usage is None:
            return 0, 0
        return usage.prompt_tokens, usage.completion_tokens
