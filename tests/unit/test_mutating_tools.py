"""Configuring a tool that changes something, and the gate that then fires.

`parse_allowed_tool` never set `mutation_enabled`, so no mutating tool could
be configured at all: the prompt, the ledger, the per-invocation approval and
the Discord flow were implemented, tested and unreachable from any
deployment. The human gate on actions that change the world had never run.

So these tests are about the two things that were missing, and only those:

*   that an operator can say "this tool writes, and I am enabling it", that
    saying it takes more than a typo, and that nothing else -- least of all
    the server's own advertisement -- can say it for them;
*   that a deployment which says it gets a confirmation prompt instead of a
    call, and the call only after the person who asked has approved it.

Everything here goes through `build_federation`, the function the bot's
entrypoint calls, rather than through hand-built collaborators: a test that
assembles its own stack cannot see the wiring defect this file exists for.
"""

from __future__ import annotations

from contextlib import AbstractContextManager

import pytest
from structlog.testing import capture_logs

from chatmemory.adapters.mcp_client.config import (
    ConfigurationError as FederationConfigurationError,
)
from chatmemory.adapters.mcp_client.routing import RoutedTools, ToolRouter
from chatmemory.app.authorization import (
    ActionOrigin,
    ConfirmationError,
    ConfirmationPrompt,
    ConfirmationState,
    CredentialScope,
    InvocationRequest,
    Refusal,
    ToolEffect,
)
from chatmemory.app.confirmation import (
    ConfirmationChannel,
    ConfirmationDesk,
    ConfirmationReply,
    Undeliverable,
    attending,
)
from chatmemory.composition import (
    MUTATION_SUFFIX,
    READ_ONLY_SUFFIX,
    FederatedTools,
    build_federation,
    parse_allowed_tool,
    parse_credential_holder,
)
from chatmemory.config import Settings
from chatmemory.domain.identity import PersonRef
from tests.unit.test_composition import settings
from tests.unit.test_federation_support import (
    ALICE,
    MALLORY,
    FakeSession,
    read_tool,
    session_factory,
    write_tool,
)

SERVER = "issues"
TOOL = "comment_issue"
QUALIFIED = f"{SERVER}:{TOOL}"
DESCRIPTION = "post a comment on an issue"
QUESTION = "post a comment on the deploy issue saying it is done"
ARGUMENTS = {"issue": 7, "comment": "done"}
HOLDER = f"{SERVER}=discord:{ALICE.platform_user_id}"


def mutating_deployment(**overrides: object) -> Settings:
    """One server, one tool the operator has enabled for mutation."""
    return settings(
        **{
            "federation_servers": f"{SERVER}=inproc://{SERVER}",
            "federation_tool_allowlist": f"{QUALIFIED}:{MUTATION_SUFFIX}",
            "federation_credential_holders": HOLDER,
            **overrides,
        }
    )


def server_offering(*, mutating: bool = True) -> dict[str, FakeSession]:
    tool = write_tool(TOOL, DESCRIPTION) if mutating else read_tool(TOOL, DESCRIPTION)
    return {SERVER: FakeSession(tools=(tool,))}


def a_call(
    requester: PersonRef = ALICE, arguments: dict[str, object] | None = None
) -> InvocationRequest:
    return InvocationRequest(
        requester=requester,
        question=QUESTION,
        qualified_name=QUALIFIED,
        arguments=dict(ARGUMENTS if arguments is None else arguments),
        origin=ActionOrigin.REQUESTER_REQUEST,
    )


def routed(tools: FederatedTools) -> RoutedTools:
    """The offer this question produces, recomputed as the invoker does."""
    return ToolRouter(5).route(QUESTION, tools.federation.registration)


class Answering:
    """A person at a keyboard, answering the one prompt they are shown.

    Records what it was shown, because "the gate fired" is a claim about the
    person having seen the tool, the server and the arguments -- not about a
    boolean that happened to come back true.
    """

    def __init__(
        self,
        granted: bool = True,
        *,
        responder: PersonRef | None = None,
        silent: bool = False,
        undeliverable: bool = False,
    ) -> None:
        self._granted = granted
        self._responder = responder
        self._silent = silent
        self._undeliverable = undeliverable
        self.seen: list[ConfirmationPrompt] = []

    @property
    def asked(self) -> int:
        return len(self.seen)

    async def ask(
        self, prompt: ConfirmationPrompt, window_seconds: float
    ) -> ConfirmationReply | None:
        self.seen.append(prompt)
        if self._undeliverable:
            raise Undeliverable("no private channel with this person")
        if self._silent:
            return None
        return ConfirmationReply(
            responder=self._responder or prompt.requester, granted=self._granted
        )


def attending_channel(
    tools: FederatedTools, surface: Answering, who: PersonRef = ALICE
) -> AbstractContextManager[None]:
    """The channel `AskService` opens around one person's question.

    Over the federation's *own* ledger, which is what makes an approval
    collected here the approval the invoke-time gate reads.
    """
    return attending(ConfirmationChannel(ConfirmationDesk(tools.confirmations), surface, who))


async def a_mutating_deployment() -> tuple[FederatedTools, FakeSession]:
    sessions = server_offering()
    tools = await build_federation(mutating_deployment(), session_factory(sessions))
    assert tools is not None, "the deployment under test did not federate"
    return tools, sessions[SERVER]


# --- the grammar -------------------------------------------------------


def test_read_only_is_what_an_entry_means_when_it_says_nothing() -> None:
    """The default posture, unchanged by adding a way to enable mutation."""
    entry = parse_allowed_tool(f"{QUALIFIED}:{READ_ONLY_SUFFIX}")

    assert entry.effect is ToolEffect.READ_ONLY
    assert not entry.mutation_enabled
    assert entry.credential is CredentialScope.NARROW_READ_ONLY


def test_an_undeclared_effect_is_mutating_and_not_enabled() -> None:
    """A typo costs a refusal, never a write."""
    entry = parse_allowed_tool(QUALIFIED)

    assert entry.effect is None
    assert not entry.mutation_enabled


def test_an_operator_can_declare_a_tool_state_changing_and_enable_it() -> None:
    entry = parse_allowed_tool(f"{QUALIFIED}:{MUTATION_SUFFIX}")

    assert entry.effect is ToolEffect.MUTATING
    assert entry.mutation_enabled
    # Per-requester, and not by choice: a write carries the authority of the
    # person who asked for it, never the bot's own narrow identity.
    assert entry.credential is CredentialScope.PER_REQUESTER


def test_enabling_mutation_costs_more_than_enabling_read_only() -> None:
    """Two letters to keep the safe default; a phrase to leave it.

    Each near-miss below is a plausible thing to type. None of them enables
    anything, and none of them silently downgrades to "read-only" either:
    they stop the deployment.
    """
    assert len(MUTATION_SUFFIX) > 4 * len(READ_ONLY_SUFFIX)
    for near_miss in ("rw", "mutate", "mutating", "enable_mutation", "enable-mutations"):
        with pytest.raises(FederationConfigurationError):
            parse_allowed_tool(f"{QUALIFIED}:{near_miss}")


def test_a_credential_holder_names_one_person_on_one_server() -> None:
    server, person = parse_credential_holder(HOLDER)

    assert server == SERVER
    assert person == ALICE


@pytest.mark.parametrize(
    "spec", ["issues", "issues=", "issues=alice", "=discord:1", "issues=discord:alice"]
)
def test_a_malformed_credential_holder_is_refused_at_load(spec: str) -> None:
    with pytest.raises(FederationConfigurationError):
        parse_credential_holder(spec)


# --- only the operator may mark a tool read-only -----------------------


async def test_a_servers_own_read_only_claim_does_not_enable_anything() -> None:
    """The server says "this one only reads". It is not the operator.

    An advertisement can tighten -- a server admitting it writes is believed
    -- but it can never relax, and it can certainly never enable a mutation
    the operator did not enable.
    """
    sessions = server_offering(mutating=False)
    tools = await build_federation(
        settings(
            federation_servers=f"{SERVER}=inproc://{SERVER}",
            federation_tool_allowlist=QUALIFIED,
        ),
        session_factory(sessions),
    )

    assert tools is not None
    permit = tools.federation.registration.permits[QUALIFIED]
    assert permit.effect is ToolEffect.UNDETERMINED
    assert permit.effect.mutates
    assert not permit.mutation_enabled

    outcome = await tools.invoker.invoke(a_call(), routed(tools))

    assert not outcome.invoked
    assert outcome.decision.refusal is Refusal.MUTATION_NOT_ENABLED
    assert sessions[SERVER].calls == []


async def test_a_declared_mutating_tool_stays_mutating_when_the_server_denies_it() -> None:
    """The operator declared the write; the server advertises a read."""
    sessions = server_offering(mutating=False)
    tools = await build_federation(mutating_deployment(), session_factory(sessions))

    assert tools is not None
    permit = tools.federation.registration.permits[QUALIFIED]
    assert permit.effect is ToolEffect.MUTATING
    assert permit.mutation_enabled


# --- what the composition root actually builds -------------------------


async def test_a_configured_mutating_tool_reaches_the_permit_the_gate_reads() -> None:
    tools, _ = await a_mutating_deployment()

    permit = tools.federation.registration.permits[QUALIFIED]
    assert permit.mutation_enabled
    assert permit.effect is ToolEffect.MUTATING
    assert permit.credential is CredentialScope.PER_REQUESTER


async def test_enabling_a_mutation_nobody_may_spend_stops_the_deployment() -> None:
    """Half a grant is a configuration error, not a quiet nothing.

    Without a declared holder every call is refused for want of a credential
    -- safe, and indistinguishable from a working configuration until
    somebody tries it. That shape is this project's recurring defect, so it
    is refused out loud at startup instead.
    """
    half = settings(
        federation_servers=f"{SERVER}=inproc://{SERVER}",
        federation_tool_allowlist=f"{QUALIFIED}:{MUTATION_SUFFIX}",
    )

    with capture_logs() as logs:
        tools = await build_federation(half, session_factory(server_offering()))

    assert tools is None
    misconfigured = [e for e in logs if e["event"] == "composition.federation.misconfigured"]
    assert misconfigured and QUALIFIED in misconfigured[0]["error"]


async def test_a_holder_on_a_server_nobody_configured_stops_the_deployment() -> None:
    stale = mutating_deployment(federation_credential_holders="docs=discord:1001")

    with capture_logs() as logs:
        assert await build_federation(stale, session_factory(server_offering())) is None

    assert [e for e in logs if e["event"] == "composition.federation.misconfigured"]


async def test_startup_says_out_loud_that_the_agent_may_change_something() -> None:
    """A read-only deployment and a writing one used to log identically."""
    with capture_logs() as logs:
        await a_mutating_deployment()

    granted = [e for e in logs if e["event"] == "composition.federation.mutation_enabled"]
    assert granted, "an operator cannot see that they granted a write"
    assert granted[0]["tools"] == [QUALIFIED]
    assert granted[0]["holders"] == [str(ALICE)]
    assert granted[0]["log_level"] == "warning"

    registered = [e for e in logs if e["event"] == "composition.federation.registered"]
    assert registered[0]["mutating"] == [QUALIFIED]
    assert registered[0]["read_only"] == []


async def test_a_read_only_deployment_reports_no_mutation() -> None:
    read_only = settings(
        federation_servers=f"{SERVER}=inproc://{SERVER}",
        federation_tool_allowlist=f"{QUALIFIED}:{READ_ONLY_SUFFIX}",
    )

    with capture_logs() as logs:
        tools = await build_federation(read_only, session_factory(server_offering()))

    assert tools is not None
    assert not [e for e in logs if e["event"] == "composition.federation.mutation_enabled"]
    registered = [e for e in logs if e["event"] == "composition.federation.registered"]
    assert registered[0]["read_only"] == [QUALIFIED]
    assert registered[0]["mutating"] == []


# --- the gate fires ----------------------------------------------------


async def test_the_gate_proposes_instead_of_calling() -> None:
    """The invoker's answer to an enabled mutation is a question."""
    tools, session = await a_mutating_deployment()

    outcome = await tools.invoker.invoke(a_call(), routed(tools))

    assert not outcome.invoked
    assert session.calls == [], "the tool ran before anyone was asked"
    assert outcome.decision.refusal is Refusal.CONFIRMATION_REQUIRED
    prompt = outcome.prompt
    assert prompt is not None
    assert prompt.requester == ALICE
    assert prompt.qualified_name == QUALIFIED
    assert prompt.server == SERVER
    # The arguments, not the tool's name: what a person approves is the call.
    assert prompt.arguments_rendered == '{"comment":"done","issue":7}'


async def test_the_call_follows_the_requesters_own_approval() -> None:
    tools, session = await a_mutating_deployment()
    first = await tools.invoker.invoke(a_call(), routed(tools))
    assert first.prompt is not None

    tools.confirmations.record(first.prompt, ALICE, granted=True)
    second = await tools.invoker.invoke(a_call(), routed(tools))

    assert second.invoked
    assert second.decision.confirmation is ConfirmationState.GRANTED
    assert session.calls == [(TOOL, ARGUMENTS)]


async def test_somebody_elses_yes_is_not_an_approval() -> None:
    tools, session = await a_mutating_deployment()
    outcome = await tools.invoker.invoke(a_call(), routed(tools))
    assert outcome.prompt is not None

    with pytest.raises(ConfirmationError):
        tools.confirmations.record(outcome.prompt, MALLORY, granted=True)

    assert not (await tools.invoker.invoke(a_call(), routed(tools))).invoked
    assert session.calls == []


async def test_approving_one_call_does_not_approve_a_different_one() -> None:
    """The digest binds the yes to these arguments.

    A run that approved "comment on issue 7" and then sent "close issue 7"
    would have a person's approval for something they never read.
    """
    tools, session = await a_mutating_deployment()
    first = await tools.invoker.invoke(a_call(), routed(tools))
    assert first.prompt is not None
    tools.confirmations.record(first.prompt, ALICE, granted=True)

    changed = await tools.invoker.invoke(
        a_call(arguments={"issue": 7, "comment": "closing this"}), routed(tools)
    )

    assert not changed.invoked
    assert changed.decision.refusal is Refusal.ARGUMENTS_CHANGED
    assert changed.prompt is not None, "the new arguments were never put to anyone"
    assert session.calls == []


async def test_a_requester_who_holds_no_credential_is_not_even_asked() -> None:
    """Enabled for Alice is not enabled for everyone.

    Refused before the confirmation, and deliberately with no prompt: a
    person who may not act must not be trained to click through one.
    """
    tools, session = await a_mutating_deployment()

    outcome = await tools.invoker.invoke(a_call(requester=MALLORY), routed(tools))

    assert not outcome.invoked
    assert outcome.prompt is None
    assert outcome.decision.refusal is Refusal.NO_REQUESTER_CREDENTIAL
    assert session.calls == []


async def test_every_attempt_is_on_the_record() -> None:
    """Refusal, approval and call, reconstructable after the fact."""
    tools, _ = await a_mutating_deployment()
    first = await tools.invoker.invoke(a_call(), routed(tools))
    assert first.prompt is not None
    tools.confirmations.record(first.prompt, ALICE, granted=True)
    await tools.invoker.invoke(a_call(), routed(tools))

    entries = tools.audit.entries()
    assert [e.outcome for e in entries] == ["refused", "invoked"]
    assert [e.confirmation for e in entries] == ["absent", "granted"]
    assert all(e.requester == str(ALICE) and e.question == QUESTION for e in entries)
    assert entries[-1].arguments == '{"comment":"done","issue":7}'
    assert tools.audit.verify()


# --- through the surface the reasoning loop holds ----------------------


async def test_the_loop_surface_asks_the_person_and_then_calls() -> None:
    """The whole path a question travels, with nothing hand-assembled.

    `build_federation` -> `RoutedToolSurface.invoke` -> `with_confirmation`
    -> the desk `AskService` attends -> the ledger the gate reads. Every test
    above could pass with the surface wired to nothing.
    """
    tools, session = await a_mutating_deployment()
    person = Answering()

    with attending_channel(tools, person):
        outcome = await tools.surface.invoke(a_call())

    assert person.asked == 1, "the surface the loop holds proposed to nobody"
    assert person.seen[0].arguments_rendered == '{"comment":"done","issue":7}'
    assert outcome.invoked
    assert session.call_names == [TOOL]


async def test_the_loop_surface_calls_nothing_when_the_person_declines() -> None:
    tools, session = await a_mutating_deployment()
    person = Answering(granted=False)

    with attending_channel(tools, person):
        outcome = await tools.surface.invoke(a_call())

    assert person.asked == 1
    assert not outcome.invoked
    assert session.calls == []


@pytest.mark.parametrize("how", [{"silent": True}, {"undeliverable": True}])
async def test_silence_and_a_closed_door_are_both_refusals(how: dict[str, bool]) -> None:
    """Every way of not getting an answer is the same outcome: no call."""
    tools, session = await a_mutating_deployment()
    person = Answering(**how)

    with attending_channel(tools, person):
        outcome = await tools.surface.invoke(a_call())

    assert not outcome.invoked
    assert session.calls == []


async def test_a_run_with_nobody_attending_changes_nothing() -> None:
    """A background path, or a deployment that wired no surface."""
    tools, session = await a_mutating_deployment()

    outcome = await tools.surface.invoke(a_call())

    assert not outcome.invoked
    assert session.calls == []


async def test_a_reply_from_someone_other_than_the_requester_changes_nothing() -> None:
    tools, session = await a_mutating_deployment()
    impostor = Answering(responder=MALLORY)

    with attending_channel(tools, impostor):
        outcome = await tools.surface.invoke(a_call())

    assert not outcome.invoked
    assert session.calls == []


async def test_a_read_only_tool_is_called_without_asking_anyone() -> None:
    """The gate is for mutations. Read-only answers must not grow a prompt."""
    read_only = settings(
        federation_servers=f"{SERVER}=inproc://{SERVER}",
        federation_tool_allowlist=f"{SERVER}:search:{READ_ONLY_SUFFIX}",
    )
    sessions = {SERVER: FakeSession(tools=(read_tool("search", "search the issues"),))}
    tools = await build_federation(read_only, session_factory(sessions))
    assert tools is not None
    person = Answering()

    with attending_channel(tools, person):
        outcome = await tools.surface.invoke(
            InvocationRequest(
                requester=ALICE,
                question="search the issues for the deploy",
                qualified_name=f"{SERVER}:search",
                arguments={"query": "search the issues for the deploy"},
                origin=ActionOrigin.REQUESTER_REQUEST,
            )
        )

    assert outcome.invoked
    assert person.asked == 0
