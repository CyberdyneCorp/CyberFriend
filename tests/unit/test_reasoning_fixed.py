"""The fixed corrective path, end to end, with fakes in place of models.

Also the home of the fakes the loop and service tests reuse: a retrieval tool
that enforces the viewer's channel set the way the real store does, a critic
reading from a script, and a synthesiser that cites whatever it is given.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from chatmemory.app.reasoning.budgets import Budget
from chatmemory.app.reasoning.contract import (
    DEPENDENCY_FAILED_TEXT,
    NOTHING_FOUND,
    AnswerPath,
    DecisionMaker,
    RunStatus,
    TerminalCause,
)
from chatmemory.app.reasoning.errors import RetrievalUnavailable
from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.app.reasoning.fixed import SUFFICIENCY, CorrectiveDriver, FixedPath
from chatmemory.app.reasoning.gate import (
    Calibration,
    CalibrationSample,
    CoverageBucket,
    RelevanceGate,
)
from chatmemory.app.reasoning.ports import Grounded, Plan, RetrievalResult
from chatmemory.app.reasoning.verdicts import Assessment, Verdict
from chatmemory.domain.audience import Audience, DeliveryMode
from chatmemory.domain.identity import ChannelRef, PersonRef, Viewer
from chatmemory.domain.search import RelevanceSource, SearchQuery
from chatmemory.ports.answers import Question

ASKER = PersonRef("discord", 1)
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def ch(channel_id: int) -> ChannelRef:
    return ChannelRef("discord", channel_id)


def question(
    text: str = "what happened with the deploy",
    visible: Sequence[int] = (100,),
    audience: Sequence[int] | None = None,
) -> Question:
    viewer = Viewer(ASKER, frozenset(ch(c) for c in visible))
    readable = frozenset(ch(c) for c in (visible if audience is None else audience))
    return Question(
        text=text,
        asker=viewer,
        audience=Audience(
            mode=DeliveryMode.DIRECT_MESSAGE,
            members=frozenset({ASKER}),
            readable_channels=readable,
        ),
    )


def evidence(
    window_id: int,
    channel: int = 100,
    score: float = 0.016,
    source: RelevanceSource = RelevanceSource.FUSED_RRF,
) -> Evidence:
    return Evidence(
        window_id=window_id,
        channel=ch(channel),
        text=f"window {window_id}",
        score=score,
        relevance_source=source,
        url=f"https://discord.com/channels/1/{channel}/{window_id}",
        author_display="someone",
        message_ids=(window_id * 10,),
    )


class FakeRetrieval:
    """Serves scripted batches, filtered by the viewer the way the store is.

    Filtering here rather than in the test is deliberate: it is what lets a
    test assert that a restricted viewer's run never sees restricted content
    at *any* step, including steps the driver chose on its own.
    """

    def __init__(
        self,
        batches: Sequence[Sequence[Evidence]] = (),
        access_blocked: bool = False,
        fail: bool = False,
    ) -> None:
        self._batches = [tuple(b) for b in batches]
        self._access_blocked = access_blocked
        self._fail = fail
        self.calls: list[tuple[Viewer, SearchQuery]] = []

    async def retrieve(self, viewer: Viewer, query: SearchQuery) -> RetrievalResult:
        if self._fail:
            raise RetrievalUnavailable("the index did not answer")
        self.calls.append((viewer, query))
        index = min(len(self.calls) - 1, len(self._batches) - 1) if self._batches else -1
        batch = self._batches[index] if index >= 0 else ()
        visible = tuple(item for item in batch if item.channel in viewer.visible_channels)
        return RetrievalResult(items=visible, access_blocked=self._access_blocked)


class ScriptedCritic:
    """Returns verdicts in order; the last one repeats."""

    def __init__(
        self,
        *verdicts: Verdict,
        model_calls: int = 1,
        tokens: int = 100,
        suggest: bool = False,
    ) -> None:
        self._verdicts = verdicts or (Verdict.SUFFICIENT,)
        self._model_calls = model_calls
        self._tokens = tokens
        # A critic that proposes a fresh query every round, as a model would.
        # Without it the deterministic reformulation converges and the run
        # stops for lack of progress before any budget is reached.
        self._suggest = suggest
        self.calls = 0

    async def assess(
        self, question_text: str, query: SearchQuery, items: Sequence[Evidence]
    ) -> Assessment:
        verdict = self._verdicts[min(self.calls, len(self._verdicts) - 1)]
        self.calls += 1
        return Assessment(
            verdict=verdict,
            model_calls=self._model_calls,
            prompt_tokens=self._tokens,
            suggested_query=f"rephrased {self.calls}" if self._suggest else None,
        )


class CitingSynthesizer:
    """Cites everything it was handed, so citation filtering is the driver's."""

    def __init__(
        self, cite: Sequence[int] | None = None, text: str = "here is what I found"
    ) -> None:
        self._cite = cite
        self._text = text
        self.calls = 0

    async def synthesize(self, question_text: str, items: Sequence[Evidence]) -> Grounded:
        self.calls += 1
        cited = tuple(self._cite) if self._cite is not None else tuple(i.window_id for i in items)
        return Grounded(text=self._text, cited_window_ids=cited, model_calls=1, prompt_tokens=200)


class FakePlanner:
    def __init__(self, *sub_questions: str, model_calls: int = 1) -> None:
        self._sub_questions = sub_questions
        self._model_calls = model_calls

    async def plan(self, question_text: str, max_steps: int) -> Plan:
        return Plan(
            sub_questions=self._sub_questions, model_calls=self._model_calls, prompt_tokens=50
        )


def build(
    retrieval: FakeRetrieval,
    critic: ScriptedCritic | None = None,
    synthesizer: CitingSynthesizer | None = None,
    budget: Budget | None = None,
    gate: RelevanceGate | None = None,
) -> FixedPath:
    driver = CorrectiveDriver(
        retrieval, critic or ScriptedCritic(), gate=gate, budget=budget or Budget()
    )
    return FixedPath(driver, synthesizer or CitingSynthesizer())


def calibrated_gate(bypass: bool = True) -> RelevanceGate:
    calibration = Calibration.from_samples(
        [
            CalibrationSample(CoverageBucket.COVERED, 0.7),
            CalibrationSample(CoverageBucket.PARTIAL, 0.5),
            CalibrationSample(CoverageBucket.UNCOVERED_FAR, 0.05),
        ]
    )
    return RelevanceGate(calibration, enabled=True, bypass_evaluation=bypass)


# --- the common case ---------------------------------------------------


async def test_sufficient_evidence_answers_without_a_second_retrieval() -> None:
    retrieval = FakeRetrieval([[evidence(1), evidence(2)]])
    outcome = await build(retrieval).run(question())

    assert len(retrieval.calls) == 1
    assert outcome.record.status is RunStatus.ANSWERED
    assert outcome.record.cause is TerminalCause.EVIDENCE_SUFFICIENT
    assert outcome.record.path is AnswerPath.FIXED
    assert {c.message_id for c in outcome.answer.citations} == {10, 20}
    assert not outcome.answer.partial


async def test_a_weak_first_pass_is_corrected_and_then_answered() -> None:
    retrieval = FakeRetrieval([[], [evidence(1)]])
    critic = ScriptedCritic(Verdict.IRRELEVANT, Verdict.SUFFICIENT)
    outcome = await build(retrieval, critic).run(question())

    assert len(retrieval.calls) == 2
    assert outcome.record.status is RunStatus.ANSWERED
    assert outcome.record.queries[0] != outcome.record.queries[1]


async def test_widening_never_changes_the_channel_preference() -> None:
    retrieval = FakeRetrieval([[evidence(1)], [evidence(2)], [evidence(3)]])
    critic = ScriptedCritic(Verdict.PARTIAL, Verdict.PARTIAL, Verdict.SUFFICIENT)
    await build(retrieval, critic).run(question())

    assert len(retrieval.calls) == 3
    assert all(query.channels is None for _, query in retrieval.calls)
    assert [query.limit for _, query in retrieval.calls] == [20, 40, 50]


# --- who the run retrieves as ------------------------------------------


async def test_retrieval_is_scoped_to_the_audience_not_the_asker() -> None:
    """A public reply may only rest on what everyone receiving it can read."""
    retrieval = FakeRetrieval([[evidence(1, channel=100), evidence(2, channel=300)]])
    asked = question(visible=(100, 300), audience=(100,))
    outcome = await build(retrieval).run(asked)

    viewer, _ = retrieval.calls[0]
    assert viewer.visible_channels == {ch(100)}
    assert {c.channel for c in outcome.answer.citations} == {ch(100)}


async def test_the_same_viewer_is_used_at_every_step() -> None:
    retrieval = FakeRetrieval([[], [], [evidence(1)]])
    critic = ScriptedCritic(Verdict.IRRELEVANT, Verdict.IRRELEVANT, Verdict.SUFFICIENT)
    await build(retrieval, critic).run(question(visible=(100, 200)))

    viewers = {frozenset(viewer.visible_channels) for viewer, _ in retrieval.calls}
    assert viewers == {frozenset({ch(100), ch(200)})}


# --- budgets, enforced by the driver -----------------------------------


async def test_a_policy_that_would_loop_forever_still_terminates() -> None:
    """The critic never concedes and the policy always asks for another go.
    The driver is what stops, and it stops at max_attempts."""
    retrieval = FakeRetrieval([[evidence(i)] for i in range(1, 20)])
    critic = ScriptedCritic(Verdict.IRRELEVANT, suggest=True)
    outcome = await build(retrieval, critic, budget=Budget(max_attempts=3)).run(question())

    assert len(retrieval.calls) == 3
    assert outcome.record.spend.attempts == 3
    assert outcome.record.cause is TerminalCause.BUDGET_EXHAUSTED
    assert outcome.answer.partial


async def test_raising_max_attempts_actually_yields_more_attempts() -> None:
    """Not silently capped by anything underneath."""
    counts = []
    for attempts in (2, 5, 9):
        retrieval = FakeRetrieval([[evidence(i)] for i in range(1, 30)])
        await build(
            retrieval,
            ScriptedCritic(Verdict.IRRELEVANT, suggest=True),
            # The other bounds raised out of the way: this test is about the
            # attempt bound alone, and the default model-call budget would
            # otherwise stop the longest run first.
            budget=Budget(max_attempts=attempts, max_model_calls=100, max_tool_calls=100),
        ).run(question())
        counts.append(len(retrieval.calls))
    assert counts == [2, 5, 9]


async def test_a_round_adding_no_new_evidence_ends_the_run() -> None:
    """Re-retrieving what the run already holds is not progress."""
    repeated = [evidence(1)]
    retrieval = FakeRetrieval([repeated, repeated, repeated])
    critic = ScriptedCritic(Verdict.IRRELEVANT, suggest=True)
    outcome = await build(retrieval, critic, budget=Budget(max_attempts=6)).run(question())

    assert len(retrieval.calls) == 2
    assert outcome.record.cause is TerminalCause.NO_PROGRESS
    assert outcome.record.spend.attempts < 6


async def test_an_equivalent_query_is_not_issued_again() -> None:
    """A repeat costs an attempt only if it is allowed to happen."""
    retrieval = FakeRetrieval([[]])
    critic = ScriptedCritic(Verdict.EMPTY)
    outcome = await build(retrieval, critic, budget=Budget(max_attempts=8)).run(
        question(text="deploy")
    )

    assert len(retrieval.calls) < 8
    assert outcome.record.spend.attempts < 8
    assert any(d.outcome == "equivalent_query" for d in outcome.record.decisions)
    assert outcome.record.cause is TerminalCause.CORPUS_EMPTY


# --- outcomes ----------------------------------------------------------


async def test_an_empty_corpus_abstains_and_that_is_a_success() -> None:
    outcome = await build(FakeRetrieval([[]])).run(question())

    assert outcome.answer.abstained
    assert outcome.answer.text == NOTHING_FOUND
    assert outcome.record.status is RunStatus.ABSTAINED
    assert outcome.record.status.successful


async def test_a_dependency_failure_is_a_failure_not_an_absence() -> None:
    outcome = await build(FakeRetrieval(fail=True)).run(question())

    assert outcome.record.status is RunStatus.FAILED
    assert not outcome.record.status.successful
    assert outcome.record.cause is TerminalCause.DEPENDENCY_FAILED
    assert outcome.answer.text == DEPENDENCY_FAILED_TEXT
    assert outcome.answer.text != NOTHING_FOUND


async def test_access_blocked_and_corpus_empty_are_indistinguishable_to_the_asker() -> None:
    """Byte-identical replies, and the same observable behaviour producing
    them: same attempts, same retrievals, same decisions, same model calls.
    Only the operator record differs."""
    empty = FakeRetrieval([[]], access_blocked=False)
    blocked = FakeRetrieval([[]], access_blocked=True)
    empty_outcome = await build(empty, ScriptedCritic(Verdict.EMPTY)).run(
        question(text="deploy", visible=(100,))
    )
    blocked_outcome = await build(blocked, ScriptedCritic(Verdict.EMPTY)).run(
        question(text="deploy", visible=())
    )

    assert empty_outcome.answer.text.encode() == blocked_outcome.answer.text.encode()
    assert empty_outcome.answer == blocked_outcome.answer
    assert len(empty.calls) == len(blocked.calls)
    assert empty_outcome.record.spend.attempts == blocked_outcome.record.spend.attempts
    assert empty_outcome.record.spend.model_calls == blocked_outcome.record.spend.model_calls

    def shape(record: object) -> list[tuple[str, str, str, int]]:
        assert hasattr(record, "decisions")
        return [
            (d.name, d.outcome, str(d.made_by), d.model_calls)
            for d in record.decisions  # type: ignore[attr-defined]
        ]

    assert shape(empty_outcome.record) == shape(blocked_outcome.record)
    assert empty_outcome.record.cause is TerminalCause.CORPUS_EMPTY
    assert blocked_outcome.record.cause is TerminalCause.ACCESS_BLOCKED


async def test_a_viewer_who_can_read_nothing_still_searches() -> None:
    """No early exit on an empty visible set.

    Skipping retrieval for a viewer who can read nothing would make that run
    observably faster than a genuine search, which is the disclosure the
    shared wording exists to close.
    """
    retrieval = FakeRetrieval([[evidence(1)]])
    outcome = await build(retrieval).run(question(visible=()))

    assert retrieval.calls, "the run must issue a real retrieval"
    assert retrieval.calls[0][0].visible_channels == frozenset()
    assert outcome.answer.abstained


# --- grounding ---------------------------------------------------------


async def test_citations_the_run_never_retrieved_are_dropped() -> None:
    retrieval = FakeRetrieval([[evidence(1)]])
    synthesizer = CitingSynthesizer(cite=[1, 999])
    outcome = await build(retrieval, synthesizer=synthesizer).run(question())

    assert [c.message_id for c in outcome.answer.citations] == [10]


async def test_an_answer_resting_on_nothing_is_not_delivered_as_an_answer() -> None:
    retrieval = FakeRetrieval([[evidence(1)]])
    synthesizer = CitingSynthesizer(cite=[], text="everyone agreed to ship on Friday")
    outcome = await build(retrieval, synthesizer=synthesizer).run(question())

    assert outcome.answer.abstained
    assert outcome.answer.text == NOTHING_FOUND
    assert any(d.name == "grounding" for d in outcome.record.decisions)


# --- provenance --------------------------------------------------------


async def test_an_empty_result_is_judged_without_a_model() -> None:
    critic = ScriptedCritic(Verdict.SUFFICIENT)
    outcome = await build(FakeRetrieval([[]]), critic).run(question())

    sufficiency = outcome.record.decisions_named(SUFFICIENCY)[0]
    assert sufficiency.made_by is DecisionMaker.EMPTY_RESULT
    assert sufficiency.model_calls == 0
    assert critic.calls == 0


async def test_the_relevance_gate_decides_with_zero_model_calls() -> None:
    """The provenance record must show the cheap signal, not a model."""
    far_below = [evidence(1, score=0.01, source=RelevanceSource.RERANKED)]
    critic = ScriptedCritic(Verdict.SUFFICIENT)
    outcome = await build(
        FakeRetrieval([far_below]), critic, gate=calibrated_gate(), budget=Budget(max_attempts=1)
    ).run(question())

    sufficiency = outcome.record.decisions_named(SUFFICIENCY)[0]
    assert sufficiency.made_by is DecisionMaker.RELEVANCE_GATE
    assert sufficiency.model_calls == 0
    assert critic.calls == 0


async def test_a_model_judged_run_attributes_the_decision_to_the_model() -> None:
    outcome = await build(FakeRetrieval([[evidence(1)]])).run(question())

    sufficiency = outcome.record.decisions_named(SUFFICIENCY)[0]
    assert sufficiency.made_by is DecisionMaker.MODEL
    assert sufficiency.model_calls == 1
    assert outcome.record.spend.model_calls >= 1


async def test_the_gate_reaches_the_same_outcome_more_cheaply() -> None:
    """The benchmark, in unit form: the same unanswerable question with the
    gate on and off. What the gate saves is a model call and its prompt
    tokens; what must not change is the outcome."""
    far_below = [evidence(1, score=0.01, source=RelevanceSource.RERANKED)]

    async def run(gate: RelevanceGate | None) -> tuple[bool, bool, int, int]:
        outcome = await build(
            FakeRetrieval([far_below]),
            ScriptedCritic(Verdict.IRRELEVANT),
            gate=gate,
            budget=Budget(max_attempts=1),
        ).run(question())
        return (
            outcome.answer.abstained,
            outcome.answer.partial,
            outcome.record.spend.model_calls,
            outcome.record.spend.prompt_tokens,
        )

    gated_abstained, gated_partial, gated_calls, gated_tokens = await run(calibrated_gate())
    plain_abstained, plain_partial, plain_calls, plain_tokens = await run(None)

    assert (gated_abstained, gated_partial) == (plain_abstained, plain_partial)
    assert gated_calls < plain_calls
    assert gated_tokens < plain_tokens
