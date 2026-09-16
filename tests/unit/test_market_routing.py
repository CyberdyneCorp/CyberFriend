"""Current-price questions go to market data, never to the corpus.

Tasks 5.6, 5.11 and 5.12 of `add-chat-indexing-and-external-sources`, driven
through `build_answer_service` -- what the bot's answer stack is built from --
with a channel that really does quote a stale price.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from chatmemory.adapters.market.quotes import Quote, Timing, render
from chatmemory.app.egress import ISO_4217_CODES, MARKET_CRYPTO_PROVIDER
from chatmemory.app.reasoning.budgets import Budget
from chatmemory.app.reasoning.contract import RunStatus
from chatmemory.app.reasoning.evidence import Evidence
from chatmemory.app.reasoning.fixed import CorrectiveDriver
from chatmemory.app.reasoning.loop import FEDERATION_CALL, ToolOutcome
from chatmemory.app.reasoning.ports import (
    ExternalTool,
    Grounded,
    PromptContext,
    ToolCall,
    ToolCompletion,
)
from chatmemory.app.reasoning.service import (
    EXTERNAL_ROUTE,
    MARKET_UNAVAILABLE,
    NO_ADVICE,
    ReasoningAnswerService,
    build_answer_service,
)
from chatmemory.app.routing import MarketKind, market_question
from chatmemory.app.self_description import describe_capabilities
from chatmemory.domain.search import RelevanceSource
from chatmemory.ports.answers import Question
from chatmemory.ports.memory import Recollection, RememberedTurn
from tests.unit.test_composition import FakeChat
from tests.unit.test_external_routing import WEB
from tests.unit.test_loop_invocation import ScriptedSurface
from tests.unit.test_reasoning_fixed import (
    FakePlanner,
    FakeRetrieval,
    ScriptedCritic,
    ch,
    question,
)

CRYPTO = ExternalTool(
    qualified_name="market_crypto:crypto_price",
    server=MARKET_CRYPTO_PROVIDER,
    description="current price of a supported crypto asset",
    input_schema={"type": "object", "properties": {"asset": {"type": "string"}}},
)

WANTS_BTC = ToolCompletion(
    call=ToolCall(name=CRYPTO.qualified_name, arguments={"asset": "BTC"}, call_id="m1"),
    prompt_tokens=20,
)

STALE = Evidence(
    window_id=7,
    channel=ch(100),
    text="BTC is at 60k, told you so",
    score=0.9,
    relevance_source=RelevanceSource.FUSED_RRF,
    url="https://discord.com/channels/1/100/70",
    author_display="ana",
    message_ids=(70,),
)

QUOTE = Quote(
    instrument="BTC",
    value=Decimal("97123.45"),
    unit="USD",
    source="CoinGecko",
    url="https://www.coingecko.com/en/coins/bitcoin",
    timing=Timing.QUOTE_TIME,
    as_of=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
)


def btc_outcome() -> ToolOutcome:
    """The figure exactly as the market adapter renders it."""
    return ToolOutcome(
        invoked=True,
        source_system=MARKET_CRYPTO_PROVIDER,
        text=render(QUOTE, "BTC: 97,123.45 USD"),
        attribution="CoinGecko",
    )


class AdvisingSynthesizer:
    """A model that answers from whatever it is given, and gives advice."""

    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(
        self, question_text: str, items: Sequence[Evidence], context: PromptContext
    ) -> Grounded:
        self.calls += 1
        return Grounded(
            text="BTC is at 60k -- you should buy now.",
            cited_window_ids=tuple(i.window_id for i in items),
            model_calls=1,
        )


def service(
    surface: ScriptedSurface, retrieval: FakeRetrieval, synthesizer: AdvisingSynthesizer
) -> ReasoningAnswerService:
    return build_answer_service(
        retrieval,
        FakeChat(),
        planner=FakePlanner(),
        synthesizer=synthesizer,  # type: ignore[arg-type]
        fixed_driver=CorrectiveDriver(retrieval, ScriptedCritic(), budget=Budget()),
        loop_driver=CorrectiveDriver(retrieval, ScriptedCritic(), budget=Budget()),
        tools=surface,
    )


async def test_a_price_quoted_in_a_channel_is_never_returned_as_the_current_price() -> None:
    surface = ScriptedSurface(CRYPTO, WEB, completion=WANTS_BTC, outcome=btc_outcome())
    retrieval = FakeRetrieval([[STALE]])
    synthesizer = AdvisingSynthesizer()

    outcome = await service(surface, retrieval, synthesizer).answer_run(
        question(text="what is the current BTC price?")
    )

    assert retrieval.calls == [], "the corpus was searched for a current price"
    assert outcome.record.status is RunStatus.ANSWERED
    assert "97,123.45 USD" in outcome.answer.text
    assert "60k" not in outcome.answer.text
    assert "quote time reported by CoinGecko" in outcome.answer.text
    assert [c.source_system for c in outcome.answer.citations] == [MARKET_CRYPTO_PROVIDER]
    assert outcome.answer.citations[0].url == QUOTE.url
    assert all(wid < 0 for wid in outcome.record.evidence_window_ids)
    assert decision_outcomes(outcome, EXTERNAL_ROUTE) == ["market_crypto"]
    # The figure is the adapter's own; no model paraphrases it.
    assert synthesizer.calls == 0
    # Clearance is minted from the asker's own words.
    assert surface.invocations[0].question == "what is the current BTC price?"


async def test_an_incidental_past_tense_does_not_send_a_current_price_to_the_corpus() -> None:
    """"I was away" is about the asker; the price asked for is still today's.

    Regression: any record marker ("was", "last", "channel") used to veto the
    market route, so the stale channel quote was retrieved, judged sufficient
    and presented as the current price.
    """
    for text in (
        "What is the current bitcoin price? I was away",
        "what's bitcoin at right now, last I heard it was 60k",
        "current BTC price in this channel?",
    ):
        surface = ScriptedSurface(CRYPTO, WEB, completion=WANTS_BTC, outcome=btc_outcome())
        retrieval = FakeRetrieval([[STALE]])

        outcome = await service(surface, retrieval, AdvisingSynthesizer()).answer_run(
            question(text=text)
        )

        assert retrieval.calls == [], f"the corpus was searched for: {text!r}"
        assert "97,123.45 USD" in outcome.answer.text
        assert "60k" not in outcome.answer.text
        assert decision_outcomes(outcome, EXTERNAL_ROUTE) == ["market_crypto"]


async def test_asked_whether_to_buy_the_answer_makes_no_recommendation() -> None:
    surface = ScriptedSurface(CRYPTO, completion=WANTS_BTC, outcome=btc_outcome())
    synthesizer = AdvisingSynthesizer()

    outcome = await service(surface, FakeRetrieval([[STALE]]), synthesizer).answer_run(
        question(text="should I buy BTC?")
    )

    assert outcome.answer.text.startswith(NO_ADVICE)
    lowered = outcome.answer.text.lower()
    for advice in ("you should", "buy now", "good time", "sell now", "hold on to"):
        assert advice not in lowered
    assert synthesizer.calls == 0


async def test_an_unavailable_provider_says_so_and_never_falls_back_to_the_corpus() -> None:
    surface = ScriptedSurface(
        CRYPTO, completion=WANTS_BTC, outcome=ToolOutcome(detail="tool_error")
    )
    retrieval = FakeRetrieval([[STALE]])

    outcome = await service(surface, retrieval, AdvisingSynthesizer()).answer_run(
        question(text="what is BTC at right now")
    )

    assert retrieval.calls == []
    assert outcome.answer.text == MARKET_UNAVAILABLE
    assert outcome.answer.citations == ()


async def test_a_deployment_without_market_tools_still_never_answers_from_the_corpus() -> None:
    retrieval = FakeRetrieval([[STALE]])
    answers = build_answer_service(
        retrieval,
        FakeChat(),
        synthesizer=AdvisingSynthesizer(),  # type: ignore[arg-type]
        fixed_driver=CorrectiveDriver(retrieval, ScriptedCritic()),
    )

    outcome = await answers.answer_run(question(text="ETH price"))

    assert retrieval.calls == []
    assert outcome.answer.text == MARKET_UNAVAILABLE


async def test_a_price_question_is_offered_market_tools_only() -> None:
    """A model reaching for the web on a price question gets nothing."""
    wants_web = ToolCompletion(
        call=ToolCall(name=WEB.qualified_name, arguments={"query": "btc price"}, call_id="x"),
    )
    surface = ScriptedSurface(CRYPTO, WEB, completion=wants_web, outcome=btc_outcome())

    outcome = await service(surface, FakeRetrieval([[STALE]]), AdvisingSynthesizer()).answer_run(
        question(text="btc price today")
    )

    assert surface.proposed == [("btc price today", (CRYPTO.qualified_name,))]
    assert surface.invocations == []
    assert "not_offered" in decision_outcomes(outcome, FEDERATION_CALL)
    assert outcome.answer.text == MARKET_UNAVAILABLE


def test_price_questions_are_recognised_and_questions_about_the_record_are_not() -> None:
    def kind(text: str) -> MarketKind | None:
        found = market_question(text, ISO_4217_CODES)
        return found.kind if found else None

    assert kind("what is BTC at") is MarketKind.CRYPTO
    assert kind("ETH price") is MarketKind.CRYPTO
    assert kind("where is the S&P 500 today") is MarketKind.INDEX
    assert kind("how much is 100 USD in BRL") is MarketKind.CONVERSION
    assert kind("convert 50 euros to reais") is MarketKind.CONVERSION
    # What somebody said about a price is a question for the corpus.
    assert kind("what did Ana say BTC would hit") is None
    assert kind("what was BTC at last month") is None
    assert kind("tell me about ethereum smart contracts in our project") is None
    # A present-tense claim on the figure outranks an incidental past tense.
    assert kind("What is the current bitcoin price? I was away") is MarketKind.CRYPTO
    assert kind("current ETH price in this channel?") is MarketKind.CRYPTO
    assert kind("how much is 100 USD in BRL now? last week it was 5") is MarketKind.CONVERSION
    assert kind("qual a cotação atual do bitcoin? semana passada estava 60k") is MarketKind.CRYPTO
    # ...but a date on a message is not a claim on the present.
    assert kind("what did Ana say about BTC today") is None


def test_self_description_mentions_market_data_only_when_it_is_registered() -> None:
    with_market = describe_capabilities(
        ["wikipedia:search", "market_crypto:crypto_price", "market_fx:convert"]
    )
    without = describe_capabilities(["wikipedia:search"])

    assert "Bitcoin and Ether prices" in with_market
    assert "currency conversion" in with_market
    assert "S&P 500" not in with_market
    assert "market data" not in without.lower()


def decision_outcomes(outcome: object, name: str) -> list[str]:
    record = outcome.record  # type: ignore[attr-defined]
    return [d.outcome for d in record.decisions_named(name)]


# --- ordinary phrasings and follow-ups -------------------------------------
#
# Regression: recognition needed a word from a short price-term list or a
# question of four words or fewer, so "how many dollars is one bitcoin" went to
# the corpus and came back with the stale "BTC is at 60k". A follow-up to a
# price question ("and now?") names no instrument at all, and with memory
# present it went to the loop, whose planner rewrote it into a corpus search
# for the BTC price.

ORDINARY_PRICE_QUESTIONS = (
    "how many dollars is one bitcoin",
    "What's 1 BTC in BRL?",
    "How much is an ether worth these days?",
    "How much is an ether worth these days? someone in the channel said 2k",
    "quanto está o bitcoin essa semana?",
)


def test_ordinary_price_phrasings_are_recognised() -> None:
    for text in ORDINARY_PRICE_QUESTIONS:
        found = market_question(text, ISO_4217_CODES)
        assert found is not None and found.kind is MarketKind.CRYPTO, text
    # Still questions about the record, not the figure.
    assert market_question("o que a Ana disse sobre bitcoin essa semana", ISO_4217_CODES) is None
    assert market_question("quanto foi o bitcoin semana passada", ISO_4217_CODES) is None
    assert market_question("we accept BTC and USD payments", ISO_4217_CODES) is None


async def test_an_ordinary_price_phrasing_never_reaches_the_corpus() -> None:
    for text in ORDINARY_PRICE_QUESTIONS:
        surface = ScriptedSurface(CRYPTO, WEB, completion=WANTS_BTC, outcome=btc_outcome())
        retrieval = FakeRetrieval([[STALE]])

        outcome = await service(surface, retrieval, AdvisingSynthesizer()).answer_run(
            question(text=text)
        )

        assert retrieval.calls == [], f"the corpus was searched for: {text!r}"
        assert "97,123.45 USD" in outcome.answer.text
        assert "60k" not in outcome.answer.text


def remembering(text: str, *earlier: str) -> Question:
    """A question asked after `earlier`, the asker's own remembered turns."""
    turns = tuple(
        RememberedTurn(
            turn_id=i,
            question=q,
            answer="BTC: 97,000.00 USD",
            asked_at=datetime(2026, 9, 16, 11, i, tzinfo=UTC),
            source_channels=frozenset(),
        )
        for i, q in enumerate(earlier, start=1)
    )
    return replace(question(text=text), memory=Recollection(turns=turns))


async def test_a_follow_up_to_a_price_question_goes_to_market_data() -> None:
    for text in ("and now?", "ok but what is it trading at", "e agora?"):
        surface = ScriptedSurface(CRYPTO, WEB, completion=WANTS_BTC, outcome=btc_outcome())
        retrieval = FakeRetrieval([[STALE]])

        outcome = await service(surface, retrieval, AdvisingSynthesizer()).answer_run(
            remembering(text, "what is BTC at?")
        )

        assert retrieval.calls == [], f"the corpus was searched for: {text!r}"
        assert "97,123.45 USD" in outcome.answer.text
        assert "60k" not in outcome.answer.text
        assert decision_outcomes(outcome, EXTERNAL_ROUTE) == ["market_crypto"]
        # The proposal is told the instrument, as a closed-vocabulary term and
        # never as the remembered text itself.
        proposed_text = surface.proposed[0][0]
        assert proposed_text.startswith(text)
        assert "BTC" in proposed_text
        assert "what is BTC at" not in proposed_text


async def test_a_chain_of_follow_ups_still_goes_to_market_data() -> None:
    surface = ScriptedSurface(CRYPTO, WEB, completion=WANTS_BTC, outcome=btc_outcome())
    retrieval = FakeRetrieval([[STALE]])

    outcome = await service(surface, retrieval, AdvisingSynthesizer()).answer_run(
        remembering("and now?", "what is ETH at?", "and now?")
    )

    assert retrieval.calls == []
    assert decision_outcomes(outcome, EXTERNAL_ROUTE) == ["market_crypto"]
    assert surface.proposed[0][0] == "and now? (following up on: ETH price)"


async def test_a_follow_up_to_anything_else_stays_on_the_corpus() -> None:
    surface = ScriptedSurface(CRYPTO, WEB, completion=WANTS_BTC, outcome=btc_outcome())
    retrieval = FakeRetrieval([[STALE]])

    outcome = await service(surface, retrieval, AdvisingSynthesizer()).answer_run(
        remembering("and now?", "what is BTC at?", "what did Ana decide about the deploy")
    )

    assert decision_outcomes(outcome, EXTERNAL_ROUTE) == []
    assert retrieval.calls != []
