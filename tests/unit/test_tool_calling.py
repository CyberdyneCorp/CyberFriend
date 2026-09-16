"""The shape of a tool call: what the model may ask for, and with what.

Two halves that only make sense together. A model can emit a call only if the
endpoint is asked in the function-calling form *and* the tool it is offered
carries an argument schema -- either half alone leaves the loop able to offer
tools and unable to call one.

The federated names are the subtle part. `server:tool` exists so two servers'
`search` can never be confused, and the function-calling API does not accept a
colon in a name. The translation is therefore tested as a round trip against
an endpoint that echoes back whatever name it was handed, rather than against
a name this file spells out: a test that hardcodes the wire spelling would
still pass if both sides were wrong in the same way.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall,
    Function,
)

from chatmemory.adapters.llm.chat import (
    CHAT_ONLY,
    FULL,
    MAX_TOOL_NAME_CHARS,
    OpenAICompatibleChat,
    wire_names,
)
from chatmemory.adapters.mcp_client.config import AllowedTool, FederationConfig, ServerConfig
from chatmemory.adapters.mcp_client.registry import ServerDiscovery, register
from chatmemory.adapters.mcp_client.routing import ToolRouter
from chatmemory.adapters.mcp_client.session import DiscoveredTool, open_session
from chatmemory.adapters.web.limits import CallBudget
from chatmemory.adapters.web.provider import WEB_QUERY_SCHEMA, WebToolSpec
from chatmemory.adapters.web.query import (
    ARG_ASKED,
    ARG_QUERY,
    MAX_QUERY_CHARS,
    QueryRefusal,
    check_query,
)
from chatmemory.adapters.web.serpapi import SerpApiProvider
from chatmemory.adapters.web.wikipedia import WikipediaProvider
from chatmemory.app.authorization import ToolEffect
from chatmemory.app.reasoning.capabilities import MissingCapabilityError, ModelCapability, Stage
from chatmemory.app.reasoning.ports import EMPTY_SCHEMA, ExternalTool, ToolCall, ToolDefinition

WIRE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
"""The function-calling API's whole name grammar. No colon in it."""

SEARCH_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"q": {"type": "string", "description": "what to search for"}},
    "required": ["q"],
    "additionalProperties": False,
}


# --- a fake endpoint ----------------------------------------------------


def a_tool_call(name: str, arguments: str, call_id: str = "call_1") -> Any:
    return ChatCompletionMessageFunctionToolCall(
        id=call_id, type="function", function=Function(name=name, arguments=arguments)
    )


def a_response(content: str | None = None, tool_calls: list[Any] | None = None) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))],
        usage=SimpleNamespace(prompt_tokens=90, completion_tokens=12),
    )


class ScriptedCompletions:
    """An endpoint that returns whatever the test told it to."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses) or [a_response(content="no tool needed")]
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]


class EchoingCompletions:
    """An endpoint that calls a tool by the exact name it was handed.

    This is what makes the name round trip meaningful: the wire spelling is
    never written down in a test, so a translation that is wrong on the way
    out and wrong again on the way in cannot pass.
    """

    def __init__(self, arguments: str = '{"q": "mars rover"}', offered: int = 0) -> None:
        self._arguments = arguments
        self._offered = offered
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        tools = kwargs.get("tools") or []
        if not tools:
            return a_response(content="nothing to call")
        sent = tools[self._offered]["function"]["name"]
        return a_response(tool_calls=[a_tool_call(sent, self._arguments)])


class FakeClient:
    def __init__(self, completions: Any) -> None:
        self.chat = SimpleNamespace(completions=completions)


def build(completions: Any, **kwargs: Any) -> OpenAICompatibleChat:
    return OpenAICompatibleChat(
        api_key="k",
        base_url="https://example.invalid/v1",
        model=kwargs.pop("model", "local-qwen3"),
        stages=kwargs.pop("stages", (Stage.FEDERATED_TOOLS,)),
        provides=kwargs.pop("provides", FULL),
        client=FakeClient(completions),  # type: ignore[arg-type]
        **kwargs,
    )


def definition(name: str, schema: dict[str, object] | None = None) -> ToolDefinition:
    return ToolDefinition(
        name=name, description=f"the {name} tool", input_schema=schema or SEARCH_SCHEMA
    )


# --- emitting a call ----------------------------------------------------


async def test_a_model_that_wants_no_tool_returns_text() -> None:
    chat = build(ScriptedCompletions(a_response(content="they shipped on Tuesday")))
    result = await chat.complete_with_tools(
        "sys", "when did they ship", [definition("wiki:search")]
    )

    assert result.text == "they shipped on Tuesday"
    assert result.call is None
    assert not result.wants_tool
    assert (result.prompt_tokens, result.completion_tokens) == (90, 12)


async def test_a_tool_call_comes_back_under_the_name_it_was_offered() -> None:
    """The round trip, against an endpoint that echoes the wire name."""
    endpoint = EchoingCompletions()
    chat = build(endpoint)
    result = await chat.complete_with_tools(
        "sys", "what is the mars rover", [definition("wiki:search")]
    )

    sent = endpoint.calls[0]["tools"][0]["function"]["name"]
    assert WIRE_NAME.fullmatch(sent), "a colon in a function name is a 400 from a strict endpoint"
    assert ":" not in sent

    assert result.wants_tool
    assert result.call == ToolCall(
        name="wiki:search", arguments={"q": "mars rover"}, call_id="call_1"
    )


async def test_the_schema_of_each_tool_is_what_is_sent_as_its_parameters() -> None:
    endpoint = ScriptedCompletions()
    chat = build(endpoint)
    await chat.complete_with_tools("sys", "q", [definition("wiki:search")])

    function = endpoint.calls[0]["tools"][0]["function"]
    assert function["parameters"] == SEARCH_SCHEMA
    assert function["description"] == "the wiki:search tool"


async def test_a_tool_with_no_stated_schema_is_sent_as_an_unconstrained_object() -> None:
    """Never as "takes no arguments": that describes a call that can only fail."""
    endpoint = ScriptedCompletions()
    chat = build(endpoint)
    await chat.complete_with_tools("sys", "q", [ToolDefinition(name="wiki:search")])

    assert endpoint.calls[0]["tools"][0]["function"]["parameters"] == {"type": "object"}


async def test_arguments_arrive_as_a_mapping_not_a_json_string() -> None:
    endpoint = EchoingCompletions(arguments='{"q": "mars", "limit": 3}')
    result = await build(endpoint).complete_with_tools("sys", "q", [definition("wiki:search")])

    assert result.call is not None
    assert result.call.arguments == {"q": "mars", "limit": 3}


async def test_an_empty_arguments_blob_is_an_empty_mapping() -> None:
    """OpenAI sends "{}" for a zero-argument call; some proxies send nothing."""
    endpoint = EchoingCompletions(arguments="")
    result = await build(endpoint).complete_with_tools("sys", "q", [definition("ping:ping")])

    assert result.call is not None
    assert result.call.arguments == {}


async def test_arguments_that_are_not_json_name_the_missing_capability() -> None:
    endpoint = EchoingCompletions(arguments="q=mars rover")
    with pytest.raises(MissingCapabilityError, match="tool_calling"):
        await build(endpoint).complete_with_tools("sys", "q", [definition("wiki:search")])


async def test_arguments_that_are_not_an_object_are_refused_rather_than_coerced() -> None:
    """A list passed on as a mapping fails as the tool's problem, not the endpoint's."""
    endpoint = EchoingCompletions(arguments='["mars rover"]')
    with pytest.raises(MissingCapabilityError, match="rather than an object"):
        await build(endpoint).complete_with_tools("sys", "q", [definition("wiki:search")])


async def test_only_the_first_of_several_calls_is_returned() -> None:
    """One call per turn: a confirmation shows a person one tool and one set
    of arguments, and a batch is how an unwanted call rides along."""
    endpoint = ScriptedCompletions(
        a_response(
            tool_calls=[
                a_tool_call("wiki_search", '{"q": "mars"}', call_id="first"),
                a_tool_call("issues_close", '{"number": 7}', call_id="second"),
            ]
        )
    )
    result = await build(endpoint).complete_with_tools("sys", "q", [definition("wiki:search")])

    assert result.call is not None
    assert result.call.call_id == "first"


async def test_a_name_that_was_never_offered_is_passed_through_unchanged() -> None:
    """So the invoke-time gate refuses an unknown tool, rather than this
    adapter guessing which offered tool was meant."""
    endpoint = ScriptedCompletions(a_response(tool_calls=[a_tool_call("delete_everything", "{}")]))
    result = await build(endpoint).complete_with_tools("sys", "q", [definition("wiki:search")])

    assert result.call is not None
    assert result.call.name == "delete_everything"


async def test_an_unrecognised_call_shape_is_read_as_no_call() -> None:
    """A provider extension we never asked for is not something to guess at."""
    endpoint = ScriptedCompletions(a_response(content="", tool_calls=[SimpleNamespace(id="1")]))
    result = await build(endpoint).complete_with_tools("sys", "q", [definition("wiki:search")])

    assert result.call is None


# --- what goes on the wire ----------------------------------------------


async def test_no_tools_means_no_tools_field_and_a_plain_answer() -> None:
    """A run where routing found nothing relevant still has a question to
    answer, and some endpoints reject an empty tools array."""
    endpoint = ScriptedCompletions(a_response(content="nobody mentioned it"))
    result = await build(endpoint).complete_with_tools("sys", "q", [])

    assert result.text == "nobody mentioned it"
    assert "tools" not in endpoint.calls[0]
    assert "tool_choice" not in endpoint.calls[0]


async def test_the_model_is_never_forced_to_call_a_tool() -> None:
    """Forcing a call would turn every federated run into an outbound
    request, which is the opposite of what the egress boundary is for."""
    endpoint = ScriptedCompletions()
    await build(endpoint).complete_with_tools("sys", "q", [definition("wiki:search")])

    assert endpoint.calls[0]["tool_choice"] == "auto"


async def test_nothing_provider_specific_is_sent() -> None:
    """The endpoint may be a self-hosted proxy: a feature it silently ignores
    is worse than one we never asked for."""
    endpoint = ScriptedCompletions()
    await build(endpoint).complete_with_tools("sys", "q", [definition("wiki:search")])

    assert set(endpoint.calls[0]) == {"model", "temperature", "messages", "tools", "tool_choice"}
    assert endpoint.calls[0]["tools"][0]["type"] == "function"


# --- translating federated names ----------------------------------------


def test_a_qualified_name_survives_the_grammar_the_api_imposes() -> None:
    mapping = wire_names(["github:search_issues", "wikipedia:summary"])

    assert set(mapping.values()) == {"github:search_issues", "wikipedia:summary"}
    for wire in mapping:
        assert WIRE_NAME.fullmatch(wire)


def test_two_names_that_flatten_onto_one_stay_distinct() -> None:
    """`issues:search` and `issues_search` translate alike. Letting them
    collide would hand a call to whichever was offered first -- the exact
    confusion server-qualification exists to prevent."""
    mapping = wire_names(["issues:search", "issues_search"])

    assert len(mapping) == 2
    assert set(mapping.values()) == {"issues:search", "issues_search"}


def test_an_over_long_name_is_cut_to_what_the_api_accepts() -> None:
    long_name = "server:" + "a" * 200
    mapping = wire_names([long_name, long_name.replace(":", "_")])

    assert len(mapping) == 2
    assert all(len(wire) <= MAX_TOOL_NAME_CHARS for wire in mapping)


async def test_two_tools_offered_under_one_name_still_go_out_as_two() -> None:
    """Pairing by position, not by looking the name back up."""
    endpoint = ScriptedCompletions()
    await build(endpoint).complete_with_tools(
        "sys", "q", [definition("wiki:search"), definition("wiki:search")]
    )

    sent = [t["function"]["name"] for t in endpoint.calls[0]["tools"]]
    assert len(set(sent)) == 2


async def test_a_collision_still_routes_each_call_to_its_own_tool() -> None:
    endpoint = EchoingCompletions(arguments="{}", offered=1)
    tools = [definition("issues:search"), definition("issues_search")]
    result = await build(endpoint).complete_with_tools("sys", "q", tools)

    assert result.call is not None
    assert result.call.name == "issues_search"


# --- refusing before the request ----------------------------------------


async def test_a_model_without_tool_calling_is_refused_before_anything_is_sent() -> None:
    """Named at the call, not discovered as an endpoint's 400."""
    endpoint = ScriptedCompletions()
    chat = build(endpoint, provides=CHAT_ONLY, stages=(Stage.ROUTE,))

    with pytest.raises(MissingCapabilityError, match=ModelCapability.TOOL_CALLING):
        await chat.complete_with_tools("sys", "q", [definition("wiki:search")])
    assert endpoint.calls == []


async def test_the_capability_is_checked_even_when_no_tool_was_routed() -> None:
    """Otherwise the misconfiguration only surfaces on the first question
    that happens to route a tool, which is the late failure boot checks exist
    to avoid."""
    endpoint = ScriptedCompletions()
    chat = build(endpoint, provides=CHAT_ONLY, stages=(Stage.ROUTE,))

    with pytest.raises(MissingCapabilityError, match=ModelCapability.TOOL_CALLING):
        await chat.complete_with_tools("sys", "q", [])
    assert endpoint.calls == []


def test_the_tool_calling_handle_refuses_to_be_built_on_an_incapable_model() -> None:
    chat = build(ScriptedCompletions(), provides=CHAT_ONLY, stages=(Stage.ROUTE,))

    with pytest.raises(MissingCapabilityError) as raised:
        chat.tool_caller()
    message = str(raised.value)
    assert ModelCapability.TOOL_CALLING in message
    assert Stage.FEDERATED_TOOLS in message
    assert "local-qwen3" in message


def test_the_tool_calling_handle_shares_the_client_and_the_model() -> None:
    chat = build(ScriptedCompletions())
    caller = chat.tool_caller()

    assert caller.model == chat.model
    assert caller._client is chat._client  # noqa: SLF001 - the sharing is the point


# --- a schema for every tool --------------------------------------------


def test_a_discovered_tool_carries_the_schema_its_server_advertised() -> None:
    advertised = DiscoveredTool(
        name="search_issues",
        description="Search issues",
        effect=ToolEffect.READ_ONLY,
        input_schema=SEARCH_SCHEMA,
    )
    registration = register(
        FederationConfig(
            servers=(ServerConfig(name="github", target="in-process"),),
            allowlist=(AllowedTool(server="github", tool="search_issues"),),
        ),
        [ServerDiscovery(name="github", tools=(advertised,))],
    )

    registered = registration.get("github:search_issues")
    assert registered is not None
    assert registered.input_schema == SEARCH_SCHEMA


async def test_a_real_mcp_server_supplies_the_schema_it_derives() -> None:
    """Against the SDK, not a fake: the field name and the shape of what a
    server actually advertises are exactly what a fake cannot check."""
    server: MCPServer = MCPServer(name="probe", version="0.1.0")

    @server.tool(name="search_issues", description="Search issues by text")
    async def search_issues(q: str) -> str:  # pragma: no cover - never invoked here
        return f"issue matching {q}"

    async with open_session(Client(server)) as session:
        discovered = {t.name: t for t in await session.list_tools()}

    schema = discovered["search_issues"].input_schema
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert "q" in properties


def test_routing_hands_the_schema_on_with_the_name() -> None:
    """A routed tool the run cannot see the arguments of is one it can only
    guess at."""
    advertised = DiscoveredTool(
        name="search_issues",
        description="Search issues by text",
        effect=ToolEffect.READ_ONLY,
        input_schema=SEARCH_SCHEMA,
    )
    registration = register(
        FederationConfig(
            servers=(ServerConfig(name="github", target="in-process"),),
            allowlist=(AllowedTool(server="github", tool="search_issues"),),
        ),
        [ServerDiscovery(name="github", tools=(advertised,))],
    )
    routed = ToolRouter(limit=3).route("search the issues", registration)

    assert [t.input_schema for t in routed.tools] == [SEARCH_SCHEMA]


def test_an_offered_tool_becomes_a_definition_under_its_qualified_name() -> None:
    offered = ExternalTool(
        qualified_name="github:search_issues",
        server="github",
        description="Search issues",
        input_schema=SEARCH_SCHEMA,
    )

    assert offered.definition == ToolDefinition(
        name="github:search_issues", description="Search issues", input_schema=SEARCH_SCHEMA
    )


def test_a_tool_nobody_described_defaults_to_no_schema_rather_than_to_none() -> None:
    assert ExternalTool("github:x", "github").input_schema == EMPTY_SCHEMA
    assert DiscoveredTool("x", "", ToolEffect.READ_ONLY).input_schema == EMPTY_SCHEMA


# --- the web tools' schema ----------------------------------------------


async def test_both_web_providers_advertise_the_query_schema() -> None:
    budget = CallBudget(3)
    wikipedia = await WikipediaProvider(budget).list_tools()
    serpapi = await SerpApiProvider("key", budget).list_tools()

    assert [t.name for t in wikipedia] == ["search", "summary"]
    for tool in (*wikipedia, *serpapi):
        assert tool.input_schema == WEB_QUERY_SCHEMA


def test_the_query_argument_is_the_one_the_provider_actually_reads() -> None:
    properties = WEB_QUERY_SCHEMA["properties"]
    assert isinstance(properties, dict)
    assert list(properties) == [ARG_QUERY]
    assert WEB_QUERY_SCHEMA["required"] == [ARG_QUERY]


def test_the_model_is_never_offered_the_askers_own_words_as_an_argument() -> None:
    """`asked` is minted from the question the person typed and supplied by
    the call site. A field for it would invite the model to write both halves
    of the comparison, which is no comparison at all."""
    properties = WEB_QUERY_SCHEMA["properties"]
    assert isinstance(properties, dict)
    assert ARG_ASKED not in properties
    assert WEB_QUERY_SCHEMA["additionalProperties"] is False


def test_a_schema_conformant_query_can_never_be_refused_for_its_length() -> None:
    properties = WEB_QUERY_SCHEMA["properties"]
    assert isinstance(properties, dict)
    assert properties[ARG_QUERY]["maxLength"] == MAX_QUERY_CHARS


def test_the_schema_asks_for_the_only_kind_of_query_the_guard_admits() -> None:
    """The regression this wording exists to prevent: a schema inviting
    elaboration would make the model obey it and be refused every time, and
    the deployment would look broken rather than guarded."""
    asked = "what is the mars rover doing"
    properties = WEB_QUERY_SCHEMA["properties"]
    assert isinstance(properties, dict)
    described = properties[ARG_QUERY]["description"]

    # What the description tells the model to do: use the asker's words.
    assert check_query(asked, "mars rover").ok
    # What it tells the model not to do, and what the guard does with it.
    elaborated = check_query(asked, "mars rover perseverance nasa latest status")
    assert elaborated.refusal is QueryRefusal.NOT_FROM_QUESTION
    assert "only words" in described


def test_a_provider_may_state_its_own_schema() -> None:
    spec = WebToolSpec(name="search", description="x", input_schema=SEARCH_SCHEMA)
    assert spec.input_schema == SEARCH_SCHEMA
    assert WebToolSpec(name="search", description="x").input_schema == WEB_QUERY_SCHEMA
