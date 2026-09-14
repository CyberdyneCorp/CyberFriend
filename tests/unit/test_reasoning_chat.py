"""Model capability validation, and the OpenAI-compatible chat adapter.

The adapter is exercised against a fake client rather than a live endpoint:
what is being tested is the refusal to start, the retry-free happy path and
the reading of a response -- none of which needs a network. The live check is
`probe()`, and what it does against two serving stacks is a gap recorded in
the change, not something a unit test can close.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from chatmemory.adapters.llm.chat import (
    CHAT_ONLY,
    FULL,
    OpenAICompatibleChat,
    capabilities_for,
)
from chatmemory.app.reasoning.capabilities import (
    MissingCapabilityError,
    ModelCapability,
    Stage,
    missing_capabilities,
    required_capabilities,
    validate_model_capabilities,
)


class FakeCompletions:
    def __init__(self, contents: list[str], tool_calls: object | None = None) -> None:
        self._contents = contents
        self._tool_calls = tool_calls
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        content = self._contents[min(len(self.calls) - 1, len(self._contents) - 1)]
        message = SimpleNamespace(content=content, tool_calls=self._tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
        )


class FakeClient:
    def __init__(self, completions: FakeCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def build(contents: list[str], **kwargs: Any) -> tuple[OpenAICompatibleChat, FakeCompletions]:
    completions = FakeCompletions(contents, kwargs.pop("tool_calls", None))
    chat = OpenAICompatibleChat(
        api_key="k",
        base_url="https://example.invalid/v1",
        model=kwargs.pop("model", "local-qwen3"),
        stages=kwargs.pop("stages", (Stage.EVALUATE,)),
        provides=kwargs.pop("provides", FULL),
        client=FakeClient(completions),  # type: ignore[arg-type]
        **kwargs,
    )
    return chat, completions


# --- what each stage needs ---------------------------------------------


def test_routing_needs_nothing_from_a_model() -> None:
    assert required_capabilities([Stage.ROUTE]) == frozenset()


def test_evaluation_and_synthesis_need_structured_output() -> None:
    needed = required_capabilities([Stage.EVALUATE, Stage.SYNTHESIZE])
    assert ModelCapability.STRUCTURED_OUTPUT in needed


def test_missing_capabilities_name_the_stages_that_wanted_them() -> None:
    missing = missing_capabilities([Stage.EVALUATE, Stage.FEDERATED_TOOLS], CHAT_ONLY)
    assert missing[ModelCapability.STRUCTURED_OUTPUT] == frozenset({Stage.EVALUATE})
    assert missing[ModelCapability.TOOL_CALLING] == frozenset({Stage.FEDERATED_TOOLS})


def test_validation_names_the_missing_capability() -> None:
    with pytest.raises(MissingCapabilityError, match="structured_output") as raised:
        validate_model_capabilities("local-qwen3", CHAT_ONLY, [Stage.EVALUATE])
    assert "local-qwen3" in str(raised.value)
    assert "evaluate" in str(raised.value)


# --- refusing to start -------------------------------------------------


def test_an_undeclared_model_is_assumed_to_do_chat_and_nothing_else() -> None:
    """Fail closed: the alternative is malformed JSON on request four
    hundred, in production, at an unrelated moment."""
    assert capabilities_for("some-self-hosted-build") == CHAT_ONLY
    assert capabilities_for("gpt-4o") == FULL


def test_constructing_against_an_incapable_model_refuses_to_start() -> None:
    with pytest.raises(MissingCapabilityError, match="structured_output"):
        OpenAICompatibleChat(
            api_key="k",
            base_url="https://example.invalid/v1",
            model="some-self-hosted-build",
            stages=(Stage.EVALUATE,),
        )


def test_a_declared_capable_model_constructs() -> None:
    chat, _ = build(["{}"])
    assert chat.model == "local-qwen3"
    assert chat.provides == FULL


# --- reading responses -------------------------------------------------


async def test_structured_output_is_requested_by_schema_and_parsed() -> None:
    chat, completions = build(['{"verdict": "sufficient", "score": 0.8}'])
    result = await chat.complete_json("sys", "user", {"type": "object"}, "evidence_verdict")

    assert result.data["verdict"] == "sufficient"
    assert (result.prompt_tokens, result.completion_tokens) == (120, 30)
    sent = completions.calls[0]["response_format"]
    assert sent["type"] == "json_schema"
    assert sent["json_schema"]["name"] == "evidence_verdict"
    assert sent["json_schema"]["strict"] is True


async def test_output_that_is_not_json_is_reported_as_a_missing_capability() -> None:
    chat, _ = build(["Sure! Here is the verdict: sufficient."])
    with pytest.raises(MissingCapabilityError, match="structured_output"):
        await chat.complete_json("sys", "user", {"type": "object"}, "evidence_verdict")


async def test_json_that_is_not_an_object_is_refused() -> None:
    chat, _ = build(["[1, 2, 3]"])
    with pytest.raises(MissingCapabilityError, match="rather than an object"):
        await chat.complete_json("sys", "user", {"type": "object"}, "evidence_verdict")


async def test_plain_text_completion_sends_no_response_format() -> None:
    chat, completions = build(["a sentence"], stages=(Stage.SYNTHESIZE,))
    result = await chat.complete_text("sys", "user")

    assert result.text == "a sentence"
    assert "response_format" not in completions.calls[0]


# --- the cheap second handle -------------------------------------------


def test_the_scorer_is_a_cheaper_model_on_the_same_client() -> None:
    chat, completions = build(["{}"], scoring_model="local-qwen3-small")
    scorer = chat.scorer()

    assert scorer.model == "local-qwen3-small"
    assert scorer._client is chat._client  # noqa: SLF001 - the sharing is the point


def test_the_scorer_is_validated_for_its_own_stage() -> None:
    """A handle that serves a stage needing nothing still cannot be turned
    into a scorer that needs structured output."""
    chat, _ = build(["{}"], provides=CHAT_ONLY, stages=(Stage.ROUTE,))
    with pytest.raises(MissingCapabilityError, match="structured_output"):
        chat.scorer()


# --- probing the live endpoint -----------------------------------------


async def test_probe_accepts_an_endpoint_that_honours_the_schema() -> None:
    chat, _ = build(['{"ok": true}'])
    await chat.probe()


async def test_probe_rejects_an_endpoint_that_ignores_the_schema() -> None:
    chat, _ = build(["{}"])
    with pytest.raises(MissingCapabilityError, match="ignored the response schema"):
        await chat.probe()


async def test_probe_checks_tool_calling_when_a_stage_needs_it() -> None:
    chat, _ = build(['{"ok": true}'], stages=(Stage.FEDERATED_TOOLS,))
    with pytest.raises(MissingCapabilityError, match="tool_calling"):
        await chat.probe()

    calling, _ = build(
        ['{"ok": true}'], stages=(Stage.FEDERATED_TOOLS,), tool_calls=[SimpleNamespace(id="1")]
    )
    await calling.probe()
