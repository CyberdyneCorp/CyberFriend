"""What each stage needs from the model, checked before anything serves.

An OpenAI-compatible endpoint may front anything -- OpenAI, a LiteLLM proxy,
a self-hosted vLLM or llama.cpp or Ollama gateway -- and structured-output
and tool-calling behaviour vary materially between them. A stage that needs
structured output from a model that only approximates it does not fail at
boot; it fails on request four hundred, in production, at an unrelated
moment, with malformed JSON.

So the composition root declares what the deployed model provides, the
enabled stages declare what they need, and the mismatch is a boot failure
naming the capability and the stages that wanted it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum

from chatmemory.app.reasoning.errors import ConfigurationError


class ModelCapability(StrEnum):
    CHAT = "chat"
    STRUCTURED_OUTPUT = "structured_output"
    """Schema-constrained JSON, not "usually returns JSON if asked nicely"."""

    TOOL_CALLING = "tool_calling"


class Stage(StrEnum):
    ROUTE = "route"
    PLAN = "plan"
    EVALUATE = "evaluate"
    SYNTHESIZE = "synthesize"
    SCORE = "score"
    FEDERATED_TOOLS = "federated_tools"


STAGE_CAPABILITIES: Mapping[Stage, frozenset[ModelCapability]] = {
    # Routing is lexical: it needs no model at all, which is why routine
    # questions do not pay for one.
    Stage.ROUTE: frozenset(),
    Stage.PLAN: frozenset({ModelCapability.CHAT, ModelCapability.STRUCTURED_OUTPUT}),
    Stage.EVALUATE: frozenset({ModelCapability.CHAT, ModelCapability.STRUCTURED_OUTPUT}),
    Stage.SYNTHESIZE: frozenset({ModelCapability.CHAT, ModelCapability.STRUCTURED_OUTPUT}),
    Stage.SCORE: frozenset({ModelCapability.CHAT, ModelCapability.STRUCTURED_OUTPUT}),
    Stage.FEDERATED_TOOLS: frozenset(
        {ModelCapability.CHAT, ModelCapability.TOOL_CALLING}
    ),
}

FIXED_PATH_STAGES = frozenset({Stage.ROUTE, Stage.EVALUATE, Stage.SYNTHESIZE})
LOOP_STAGES = FIXED_PATH_STAGES | {Stage.PLAN}

SCORING_STAGES = frozenset({Stage.SCORE})
"""The cheap handle's stages.

Per-candidate scoring fires once per surviving candidate and needs none of
the deliberation the main judge does, so it runs on a second, cheaper model
on the same provider.
"""


class MissingCapabilityError(ConfigurationError):
    """Raised at construction. Names the capability, never just "unsupported"."""


def required_capabilities(stages: Iterable[Stage]) -> frozenset[ModelCapability]:
    out: set[ModelCapability] = set()
    for stage in stages:
        out |= STAGE_CAPABILITIES[stage]
    return frozenset(out)


def missing_capabilities(
    stages: Iterable[Stage], provides: Iterable[ModelCapability]
) -> Mapping[ModelCapability, frozenset[Stage]]:
    """Each missing capability, mapped to the stages that wanted it."""
    available = frozenset(provides)
    wanted: dict[ModelCapability, set[Stage]] = {}
    for stage in stages:
        for capability in STAGE_CAPABILITIES[stage] - available:
            wanted.setdefault(capability, set()).add(stage)
    return {capability: frozenset(s) for capability, s in wanted.items()}


def validate_model_capabilities(
    model: str, provides: Iterable[ModelCapability], stages: Iterable[Stage]
) -> None:
    """Refuse to start when the configured model cannot serve an enabled stage."""
    stages = tuple(stages)
    missing = missing_capabilities(stages, provides)
    if not missing:
        return
    detail = "; ".join(
        f"{capability} (required by {', '.join(sorted(str(s) for s in wanting))})"
        for capability, wanting in sorted(missing.items(), key=lambda kv: str(kv[0]))
    )
    raise MissingCapabilityError(
        f"model {model!r} is missing required capability: {detail}"
    )
