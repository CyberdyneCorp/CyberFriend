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

Tool calling is the third shape, and it is only a *shape*: `complete_with_tools`
returns the model's request to call a named tool, never a call. Two details
here are wire-level and belong nowhere else -- the function-calling API's name
grammar, which a server-qualified `server:tool` violates, and the arguments
blob, which arrives as a JSON string and is parsed here so that everything
above works in mappings.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import cast

import structlog
from openai import APIError, AsyncOpenAI, RateLimitError
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageFunctionToolCall,
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
from chatmemory.app.reasoning.ports import (
    JsonCompletion,
    TextCompletion,
    ToolCall,
    ToolCompletion,
    ToolDefinition,
)

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


MAX_TOOL_NAME_CHARS = 64
_UNWIRABLE = re.compile(r"[^A-Za-z0-9_-]")


def wire_names(names: Sequence[str]) -> Mapping[str, str]:
    """Map each offered tool onto a name the function-calling API accepts.

    Federated tools are named `server:tool`, on purpose: two servers' `search`
    must never be confusable. The function-calling API takes a narrower
    grammar -- letters, digits, underscore and dash, at most 64 characters --
    so the qualified name cannot go on the wire as it stands. A strict
    endpoint answers 400; a lax one accepts it and echoes back something we
    then have to guess at, which is worse.

    So the translation happens here and is reversed on the way in, keying on
    what was actually sent rather than re-deriving it. Returns wire name ->
    offered name.
    """
    mapping: dict[str, str] = {}
    for name in names:
        base = _UNWIRABLE.sub("_", name)[:MAX_TOOL_NAME_CHARS] or "tool"
        wire, suffix = base, 2
        while wire in mapping:
            # `issues:search` and `issues_search` flatten onto one wire name.
            # Letting them collide would hand a call to whichever tool was
            # offered first -- exactly the confusion qualification exists to
            # prevent -- so the second one is made distinct instead.
            tail = f"_{suffix}"
            wire = base[: MAX_TOOL_NAME_CHARS - len(tail)] + tail
            suffix += 1
        mapping[wire] = name
    return mapping


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

    def tool_caller(
        self, stages: Iterable[Stage] = (Stage.FEDERATED_TOOLS,)
    ) -> OpenAICompatibleChat:
        """The same model, declared for a stage that calls tools.

        A composition root that federates tools builds this at startup and
        gets `MissingCapabilityError` there -- naming `tool_calling` and the
        stage that wanted it -- rather than an endpoint's 400 on the first
        question that happens to route a tool. It is the same check
        `scorer()` makes for structured output, applied to the capability the
        federated path needs.

        The alternative to building it here is to list `FEDERATED_TOOLS`
        among the answering stages, which does the same thing; what must not
        happen is neither.
        """
        return OpenAICompatibleChat(
            api_key=self._api_key,
            base_url=self._base_url,
            model=self._model,
            stages=stages,
            provides=self._provides,
            scoring_model=self._scoring_model,
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

    async def complete_with_tools(
        self, system: str, user: str, tools: Sequence[ToolDefinition]
    ) -> ToolCompletion:
        """Answer, or ask for one of `tools` to be called.

        The capability is checked before anything is sent, and before the
        empty-`tools` shortcut, because a run on the federated path with a
        model that cannot call tools is a misconfiguration whether or not
        this particular question routed one. Discovering that only on the
        first question that does is the same late failure the boot check
        exists to avoid.

        Only the plain function-calling API is used: no strict mode, no
        parallel-call toggle, no provider extension. The endpoint may be a
        self-hosted proxy, and a feature it silently ignores is worse than
        one we never asked for.
        """
        if ModelCapability.TOOL_CALLING not in self._provides:
            raise MissingCapabilityError(
                f"model {self._model!r} was not declared to provide "
                f"{ModelCapability.TOOL_CALLING}, which this call requires"
            )
        if not tools:
            # Not an error, and not an empty `tools` array on the wire: some
            # endpoints reject one, and a run where routing found nothing
            # relevant still has a question to answer.
            answer = await self.complete_text(system, user)
            return ToolCompletion(
                text=answer.text,
                prompt_tokens=answer.prompt_tokens,
                completion_tokens=answer.completion_tokens,
            )

        offered = wire_names([tool.name for tool in tools])
        # Paired by position rather than by looking each name back up: one
        # wire name is minted per offered tool, in order, so two tools that
        # happened to share a name still go out as two distinct functions.
        response = await self._create_with_tools(
            system,
            user,
            [self._tool_param(wire, tool) for wire, tool in zip(offered, tools, strict=True)],
        )
        prompt_tokens, completion_tokens = self._usage(response)
        return ToolCompletion(
            text=self._content(response),
            call=self._tool_call(response, offered),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def _tool_param(self, wire: str, tool: ToolDefinition) -> ChatCompletionToolParam:
        return cast(
            ChatCompletionToolParam,
            {
                "type": "function",
                "function": {
                    "name": wire,
                    "description": tool.description,
                    # An unstated schema is sent as an unconstrained object,
                    # never as "no arguments": telling a model a search tool
                    # takes nothing produces a call that can only fail.
                    "parameters": dict(tool.input_schema) or {"type": "object"},
                },
            },
        )

    def _tool_call(self, response: ChatCompletion, offered: Mapping[str, str]) -> ToolCall | None:
        calls = response.choices[0].message.tool_calls if response.choices else None
        if not calls:
            return None
        functions = [c for c in calls if isinstance(c, ChatCompletionMessageFunctionToolCall)]
        if not functions:
            # A call in some other shape -- a provider extension we did not
            # ask for. Not something to guess at: treat it as no call, which
            # the loop already knows how to handle.
            log.warning("chat.tool_call_unrecognised", model=self._model)
            return None
        if len(functions) > 1:
            # One call per turn is the contract above: the confirmation flow
            # shows a person one tool, one target and one set of arguments.
            log.info("chat.tool_calls_dropped", model=self._model, dropped=len(functions) - 1)
        first = functions[0]
        return ToolCall(
            # Back to the offered name, keyed on what was sent rather than
            # re-derived: a name we cannot place is returned unchanged so the
            # invoke-time gate refuses an unknown tool, instead of this
            # module inventing a plausible one.
            name=offered.get(first.function.name, first.function.name),
            arguments=self._arguments(first.function.name, first.function.arguments),
            call_id=first.id,
        )

    def _arguments(self, tool: str, raw: str) -> Mapping[str, object]:
        """Read the arguments blob, which the API sends as a JSON string."""
        text = (raw or "").strip()
        if not text:
            # OpenAI sends "{}" for a call with no arguments; some proxies
            # send nothing at all. Both mean the same thing.
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MissingCapabilityError(
                f"model {self._model!r} returned arguments for {tool!r} that are not JSON; "
                f"the endpoint does not honour {ModelCapability.TOOL_CALLING}"
            ) from exc
        if not isinstance(parsed, dict):
            # Reported rather than coerced: a list of arguments would be
            # passed to a tool as a mapping it does not have, and the failure
            # would surface as the tool's, not as the endpoint's.
            raise MissingCapabilityError(
                f"model {self._model!r} returned {type(parsed).__name__} rather than an "
                f"object as arguments for {tool!r}"
            )
        return cast(Mapping[str, object], parsed)

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
        messages = self._messages(system, user)
        if response_format is None:
            return await self._send(
                lambda: self._client.chat.completions.create(
                    model=self._model, temperature=self._temperature, messages=messages
                )
            )
        return await self._send(
            lambda: self._client.chat.completions.create(
                model=self._model,
                temperature=self._temperature,
                messages=messages,
                response_format=response_format,
            )
        )

    async def _create_with_tools(
        self, system: str, user: str, tools: Sequence[ChatCompletionToolParam]
    ) -> ChatCompletion:
        messages = self._messages(system, user)
        return await self._send(
            lambda: self._client.chat.completions.create(
                model=self._model,
                temperature=self._temperature,
                messages=messages,
                tools=list(tools),
                # "auto", not "required": the model must be free to answer
                # from what it already has. Forcing a call would turn every
                # federated run into an outbound request, which is the
                # opposite of what the egress boundary is for.
                tool_choice="auto",
            )
        )

    def _messages(self, system: str, user: str) -> list[ChatCompletionMessageParam]:
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    async def _send(
        self, attempt: Callable[[], Awaitable[ChatCompletion]]
    ) -> ChatCompletion:
        """Retry one request shape with backoff.

        Takes a factory rather than a coroutine: a coroutine can only be
        awaited once, so retrying one would raise instead of retrying.
        """
        delay = 1.0
        for number in range(self._max_retries):
            try:
                return await attempt()
            except (RateLimitError, APIError) as exc:
                if number == self._max_retries - 1:
                    raise
                log.warning("chat.retry", attempt=number + 1, error=type(exc).__name__)
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
