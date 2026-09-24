"""From a recognised request to a proposal, and from a Confirm to stored alerts.

Driven over an in-memory store with the Postgres store's rules (cap, one watch
per target, unknown people refused) and a scripted chain read, so each rule of
the proposal is pinned on its own: where the address comes from, the bounds,
the cap, duplicates, chains that could not be read, the baseline shown, the
wallet shown only in a direct message, and the language.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from chatmemory.adapters.discord.acl import DiscordAclResolver, DiscordAudienceResolver
from chatmemory.app.alert_intent import AlertIntent
from chatmemory.app.alert_requests import AlertRequests, alert_language, target_label
from chatmemory.app.alerts import AlertService
from chatmemory.app.ask import AskService
from chatmemory.app.facts import PersonalFactsService
from chatmemory.app.limits import RateLimiter
from chatmemory.domain.identity import PersonRef
from chatmemory.ports.alerts import (
    AddressSource,
    AlertKind,
    AlertLanguage,
    AlertRefusal,
    AlertState,
    HealthCandidate,
    LpCandidate,
    LpProtocol,
    LpTarget,
    NewAlert,
    PositionAlert,
    TargetsRead,
)
from tests.unit.test_facts import FakeFactStore
from tests.unit.test_facts_behaviour import (
    GENERAL,
    RecordingAnswers,
    guild,
    in_channel,
    in_dm,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LEO = PersonRef("discord", 1)
SAVED = "0xdd8a0000000000000000000000000000000063d6"
TYPED = "0xb26b933a075fbb3d4e8b0925cad4f2bc345475e0"
EN, PT = AlertLanguage.ENGLISH, AlertLanguage.PORTUGUESE
TARGET = LpTarget(LpProtocol.UNISWAP_V3, 4558452, "0xpool", "WETH", "USDC", 18, 6, 500)


class Store:
    """`AlertStore` with the rules the Postgres one enforces."""

    def __init__(self, cap: int = 10, known: frozenset[PersonRef] = frozenset({LEO})) -> None:
        self.rows: list[PositionAlert] = []
        self.cap = cap
        self.known = known

    async def create(
        self, alert: NewAlert, first_check_at: datetime
    ) -> PositionAlert | AlertRefusal:
        if alert.person not in self.known:
            return AlertRefusal.UNKNOWN_PERSON
        mine = [r for r in self.rows if r.person == alert.person and r.active]
        if len(mine) >= self.cap:
            return AlertRefusal.AT_CAP
        if any(_same(r, alert) for r in mine):
            return AlertRefusal.DUPLICATE
        row = PositionAlert(
            id=len(self.rows) + 1,
            person=alert.person,
            kind=alert.kind,
            chain=alert.chain,
            address=alert.address,
            language=alert.language,
            state=alert.state,
            state_since=NOW,
            created_at=NOW,
            next_check_at=first_check_at,
            address_source=alert.address_source,
            lp=alert.lp,
            threshold=alert.threshold,
            price=alert.price,
            edge_percent=alert.edge_percent,
            last_value=alert.last_value,
        )
        self.rows.append(row)
        return row

    async def for_person(self, person: PersonRef) -> list[PositionAlert]:
        return [r for r in self.rows if r.person == person]

    async def delete(self, person: PersonRef, alert_id: int) -> bool:
        before = len(self.rows)
        self.rows = [r for r in self.rows if not (r.id == alert_id and r.person == person)]
        return len(self.rows) < before

    async def claim_due(self, *_: Any) -> list[PositionAlert]:
        return []

    async def record(self, *_: Any) -> None:
        return None

    async def disable(self, *_: Any) -> int:
        return 0


def _same(row: PositionAlert, alert: NewAlert) -> bool:
    token = (row.lp.token_id if row.lp else -1, alert.lp.token_id if alert.lp else -1)
    return (row.kind, row.chain, row.address, row.threshold, row.price) == (
        alert.kind, alert.chain, alert.address, alert.threshold, alert.price
    ) and token[0] == token[1]


class Chain:
    """`AlertTargets`, scripted: what the read finds, and what it was asked."""

    def __init__(self, found: TargetsRead) -> None:
        self.found = found
        self.asked: list[tuple[str, AlertKind, str | None]] = []

    async def read(self, address: str, kind: AlertKind, chain: str | None) -> TargetsRead:
        self.asked.append((address, kind, chain))
        return self.found


def debt(chain: str = "base", health: str | None = "1.43") -> HealthCandidate:
    value = Decimal(health) if health is not None else None
    return HealthCandidate(chain, value)


def lp(token_id: int = 4558452, state: AlertState = AlertState.IN_RANGE) -> LpCandidate:
    return LpCandidate(
        chain="base",
        target=replace(TARGET, token_id=token_id),
        state=state,
        tick=-197404,
        price=Decimal("2674.99"),
        price_lower=Decimal("2156.02"),
        price_upper=Decimal("3149.50"),
        base_symbol="WETH",
        quote_symbol="USDC",
    )


def requests(
    found: TargetsRead, store: Store | None = None
) -> tuple[AlertRequests, Store, Chain]:
    store = store or Store()
    chain = Chain(found)
    service = AlertService(store)  # type: ignore[arg-type]
    return AlertRequests(service, chain, cap=store.cap, clock=lambda: NOW), store, chain


def health(threshold: str | None = "1.3", **changes: Any) -> AlertIntent:
    value = Decimal(threshold) if threshold is not None else None
    return AlertIntent(AlertKind.AAVE_HEALTH, value, **changes)


RANGE = AlertIntent(AlertKind.LP_RANGE)


async def propose(
    flow: AlertRequests,
    intent: AlertIntent,
    *,
    saved: str | None = SAVED,
    language: AlertLanguage = EN,
    direct: bool = True,
) -> Any:
    return await flow.propose(LEO, intent, saved_wallet=saved, language=language, direct=direct)


# --- the address ----------------------------------------------------------------


async def test_the_saved_wallet_is_used_when_none_was_typed() -> None:
    flow, _, chain = requests(TargetsRead(health=(debt(),)))

    reply = await propose(flow, health())

    assert chain.asked == [(SAVED, AlertKind.AAVE_HEALTH, None)]
    assert reply.proposal.alerts[0].address_source is AddressSource.SAVED


async def test_a_typed_address_wins_and_is_marked_typed() -> None:
    flow, _, chain = requests(TargetsRead(health=(debt(),)))

    reply = await propose(flow, health(address=TYPED, chain="base"))

    assert chain.asked == [(TYPED, AlertKind.AAVE_HEALTH, "base")]
    assert reply.proposal.alerts[0].address_source is AddressSource.TYPED


async def test_no_address_at_all_asks_for_one_and_reads_nothing() -> None:
    flow, _, chain = requests(TargetsRead(health=(debt(),)))

    reply = await propose(flow, health(), saved=None)

    assert reply.proposal is None and chain.asked == []
    assert reply.text.startswith("Which wallet?")


# --- bounds and the cap ---------------------------------------------------------


@pytest.mark.parametrize(
    ("threshold", "language", "expected"),
    [
        ("0.9", EN, "between 1.05 and 5.00, and 0.9 is outside that"),
        ("10", EN, "between 1.05 and 5.00, and 10.00 is outside that"),
        ("0.9", PT, "entre 1,05 e 5,00, e 0,9 está fora disso"),
        (None, EN, "Below what health factor?"),
        (None, PT, "Abaixo de qual health factor?"),
    ],
)
async def test_a_limit_out_of_bounds_or_missing_is_refused_with_the_bounds(
    threshold: str | None, language: AlertLanguage, expected: str
) -> None:
    flow, _, chain = requests(TargetsRead(health=(debt(),)))

    reply = await propose(flow, health(threshold), language=language)

    assert reply.proposal is None and chain.asked == []
    assert expected in reply.text


async def test_at_the_cap_nothing_is_read_or_offered() -> None:
    store = Store(cap=1)
    flow, _, chain = requests(TargetsRead(health=(debt(),)), store)
    await flow.confirm((await propose(flow, health())).proposal)

    reply = await propose(flow, health("1.5"))

    assert reply.proposal is None and chain.asked[1:] == []
    assert "You already have 1 alerts" in reply.text


async def test_more_positions_than_room_are_cut_and_say_so() -> None:
    store = Store(cap=2)
    flow, _, _ = requests(TargetsRead(lp=(lp(1), lp(2), lp(3))), store)

    reply = await propose(flow, RANGE)

    assert [a.lp.token_id for a in reply.proposal.alerts] == [1, 2]
    assert "Only 2 fit under your limit of 2 alerts" in reply.text


async def test_a_watch_that_exists_is_left_out_and_said_to_be() -> None:
    flow, _, _ = requests(TargetsRead(lp=(lp(1), lp(2))))
    first = await propose(flow, replace(RANGE, token_id=1))
    await flow.confirm(first.proposal)

    again = await propose(flow, RANGE)
    everything = await propose(flow, replace(RANGE, token_id=1))

    assert [a.lp.token_id for a in again.proposal.alerts] == [2]
    assert "1 of these are already being watched" in again.text
    assert everything.proposal is None
    assert everything.text.startswith("I'm already watching all of that.")


# --- what the read found --------------------------------------------------------


async def test_the_confirmation_lists_each_target_with_its_baseline() -> None:
    flow, _, _ = requests(TargetsRead(health=(debt("base", "1.43"), debt("arbitrum", "1.20"))))

    reply = await propose(flow, health("1.3"))

    lines = reply.text.splitlines()
    assert "• **Base** — Aave health factor below **1.30** · now **1.43**" in lines
    assert (
        "• **Arbitrum** — Aave health factor below **1.30** · now **1.20**, already "
        "below: I'll message you when it recovers"
    ) in lines
    states = [(a.chain, a.state, a.last_value) for a in reply.proposal.alerts]
    assert states == [
        ("base", AlertState.OK, Decimal("1.43")),
        ("arbitrum", AlertState.BELOW, Decimal("1.20")),
    ]
    assert "not liquidation protection" in reply.text


async def test_a_chain_with_no_debt_is_not_offered_and_none_at_all_is_said() -> None:
    flow, _, _ = requests(TargetsRead(health=(debt("ethereum", None), debt("base"))))
    none, _, _ = requests(TargetsRead(health=(debt("base", None),)))

    reply = await propose(flow, health())
    nothing = await propose(none, health(chain="base"))

    assert [a.chain for a in reply.proposal.alerts] == ["base"]
    assert nothing.proposal is None
    assert nothing.text == "There's no Aave debt on Base, so there's no health factor to watch."


async def test_a_range_proposal_shows_the_range_and_says_later_positions_are_not_covered() -> None:
    flow, _, _ = requests(TargetsRead(lp=(lp(state=AlertState.OUT_OF_RANGE),)))

    reply = await propose(flow, RANGE, language=PT)

    assert (
        "• **Base** — Uniswap v3 WETH/USDC 0,05% #4558452, faixa 2.156,02 – 3.149,50 USDC "
        "por WETH · agora fora da faixa em 2.674,99: te aviso quando voltar para a faixa"
    ) in reply.text
    assert "Posições que você abrir depois não entram" in reply.text
    [alert] = reply.proposal.alerts
    assert (alert.lp, alert.state, alert.last_value) == (
        TARGET, AlertState.OUT_OF_RANGE, Decimal(-197404)
    )


async def test_a_numbered_position_that_is_not_there_is_said_to_be_missing() -> None:
    flow, _, _ = requests(TargetsRead(lp=(lp(1),)))

    reply = await propose(flow, replace(RANGE, token_id=7))

    assert reply.proposal is None
    assert reply.text == "I don't see an open position #7."


async def test_chains_that_could_not_be_read_are_named_not_reported_empty() -> None:
    partial, _, _ = requests(TargetsRead(health=(debt(),), unreachable=("arbitrum",)))
    failed, _, _ = requests(TargetsRead(unreachable=("ethereum", "base", "arbitrum")))

    some = await propose(partial, health())
    none = await propose(failed, health())

    assert "I couldn't read Arbitrum just now, so nothing there is included." in some.text
    assert none.proposal is None
    assert none.text.startswith("I couldn't read the chain just now")


@pytest.mark.parametrize(("direct", "shown"), [(True, True), (False, False)])
async def test_the_wallet_is_named_only_in_a_direct_message(direct: bool, shown: bool) -> None:
    flow, _, _ = requests(TargetsRead(health=(debt(),)))

    reply = await propose(flow, health(), direct=direct)

    assert (SAVED in reply.text) is shown


# --- confirming -----------------------------------------------------------------


async def test_confirm_creates_what_was_listed_one_sweep_out() -> None:
    flow, store, _ = requests(TargetsRead(health=(debt(),)))
    proposal = (await propose(flow, health(), language=PT)).proposal

    said = await flow.confirm(proposal)

    [row] = store.rows
    assert (row.kind, row.threshold, row.language, row.state) == (
        AlertKind.AAVE_HEALTH, Decimal("1.3"), PT, AlertState.OK
    )
    assert row.next_check_at == NOW + timedelta(seconds=300)
    assert said.startswith("Pronto. Estou acompanhando:\n• **1** — health factor no Aave")


async def test_confirm_twice_creates_once_and_says_why() -> None:
    flow, store, _ = requests(TargetsRead(health=(debt(),)))
    proposal = (await propose(flow, health())).proposal

    await flow.confirm(proposal)
    again = await flow.confirm(proposal)

    assert len(store.rows) == 1
    assert again.startswith("Nothing new was set up.")
    assert "1 were already being watched." in again


async def test_somebody_with_no_record_is_told_rather_than_invented() -> None:
    flow, store, _ = requests(TargetsRead(health=(debt(),)), Store(known=frozenset()))
    proposal = (await propose(flow, health())).proposal

    said = await flow.confirm(proposal)

    assert store.rows == []
    assert said.startswith("I couldn't set that up: I have no record of you yet.")


# --- language and labels --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "preferred", "language"),
    [
        ("alert me if my health factor drops below 1.3", None, EN),
        ("me avisa se o health factor cair abaixo de 1,3", None, PT),
        ("hf < 1.2 ping me if", "Portuguese", PT),
        ("hf < 1.2 ping me if", None, EN),
    ],
)
def test_the_language_is_the_messages_then_the_preferred_one(
    text: str, preferred: str | None, language: AlertLanguage
) -> None:
    assert alert_language(text, preferred) is language


def test_labels_read_the_same_in_the_listing_and_the_confirmation() -> None:
    assert target_label(AlertKind.LP_RANGE, "base", TARGET, None, EN) == (
        "Uniswap v3 WETH/USDC 0.05% #4558452 on Base"
    )
    assert target_label(AlertKind.AAVE_HEALTH, "arbitrum", None, Decimal("1.3"), PT) == (
        "health factor no Aave na Arbitrum abaixo de 1,30"
    )


# --- in the ask path ------------------------------------------------------------


def _ask(flow: AlertRequests | None) -> tuple[AskService, RecordingAnswers]:
    """The ask service as the bot builds it, with `flow` as its alert requests."""
    g = guild()
    answers = RecordingAnswers()
    service = AskService(
        acl=DiscordAclResolver(g, (GENERAL,)),
        audiences=DiscordAudienceResolver(g, (GENERAL,)),
        answers=answers,  # type: ignore[arg-type]
        limiter=RateLimiter(),
        facts=PersonalFactsService(FakeFactStore()),
        alerts=flow,
    )
    return service, answers


async def test_a_request_is_proposed_before_any_answer_path_runs() -> None:
    flow, store, chain = requests(TargetsRead(health=(debt(),)))
    service, answers = _ask(flow)
    await service.ask(in_dm(f"my wallet is {SAVED}"))

    outcome = await service.ask(in_dm("me avisa se o health factor cair abaixo de 1,3"))

    assert answers.seen == [], "an alert request never reaches the answer services"
    assert outcome.alert is not None and outcome.alert.language is PT
    assert outcome.scoped.answer.text == outcome.alert.text
    assert chain.asked == [(SAVED, AlertKind.AAVE_HEALTH, None)]
    assert store.rows == [], "nothing is stored before Confirm"


async def test_in_a_channel_the_saved_wallet_is_used_but_not_shown() -> None:
    flow, _, _ = requests(TargetsRead(health=(debt(),)))
    service, _ = _ask(flow)
    await service.ask(in_dm(f"my wallet is {SAVED}"))

    outcome = await service.ask(in_channel("alert me if my health factor drops below 1.3"))

    assert outcome.alert is not None
    assert SAVED not in outcome.scoped.answer.text


async def test_with_alerts_off_a_request_is_told_so_and_not_answered() -> None:
    service, answers = _ask(None)

    outcome = await service.ask(in_dm("tell me when my LP goes out of range"))

    assert answers.seen == [] and outcome.alert is None
    assert outcome.scoped.answer.text.startswith("Alerts aren't available")


@pytest.mark.parametrize("alerts_on", [True, False])
async def test_a_scheduled_run_of_an_alert_request_is_answered_as_a_question(
    alerts_on: bool,
) -> None:
    """Nobody is present to press Confirm on a scheduled run, so an alert-shaped
    task question is answered as before: no chain read, no proposal, and no
    "not available" or refusal text sent on every interval."""
    flow, store, chain = requests(TargetsRead())
    service, answers = _ask(flow if alerts_on else None)
    question = "tell me when my LP goes out of range"

    outcome = await service.ask(in_dm(question), metered=False)

    assert [q.text for q in answers.seen] == [question]
    assert outcome.alert is None
    assert chain.asked == [] and store.rows == []


async def test_a_question_about_alerts_still_reaches_the_answers() -> None:
    service, answers = _ask(None)

    await service.ask(in_dm("what did people say about alerts?"))

    assert [q.text for q in answers.seen] == ["what did people say about alerts?"]
