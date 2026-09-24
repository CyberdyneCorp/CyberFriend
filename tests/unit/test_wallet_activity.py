"""Wallet activity: the classifier, the explorer cursor, the window and the answer.

The fixtures are real rows reduced (`activity_rows`): a week of an EIP-7702
wallet whose relayed actions the address list does not show, and a day of
address poisoning Blockscout rated "ok" throughout.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from chatmemory.adapters.chain.aave import Wrapper, Wrapping
from chatmemory.adapters.chain.activity import (
    Action,
    ChainActivity,
    Hidden,
    Judged,
    Kind,
    Recognised,
    judge,
    looks_like,
    needs_selectors,
    transactions,
)
from chatmemory.adapters.chain.activity_explorer import (
    activity_rows,
    multicall_selectors,
    next_cursor,
)
from chatmemory.adapters.chain.activity_render import MAX_LINES, render_activity
from chatmemory.adapters.chain.deployments import DEPLOYMENTS, NATIVE
from chatmemory.adapters.chain.positions import TokenInfo
from chatmemory.adapters.chain.tokens import ARBITRUM, BASE, ETHEREUM
from chatmemory.app.language import Language
from chatmemory.app.wallet_activity import MAX_DAYS, ActivityWindow, activity_window
from tests.unit.activity_rows import (
    AUSDC,
    AWETH,
    DEBT_USDC,
    DEBT_USDC_TOKEN,
    GATEWAY,
    LOOKALIKE_1,
    LP_SENT_TX,
    OTHER_WALLET,
    REAL_PAYEE,
    RELAYER_FEES,
    USDC,
    USDC_TOKEN,
    V3_MANAGER,
    WALLET,
    WETH,
    ZERO,
    ZERO_VALUE_LOOKALIKE,
    ZERO_VALUE_REAL,
    base_week,
    erc20,
    native,
    nft,
    poisoned_day,
)

BASE_DEPLOYMENT = DEPLOYMENTS[1]
USDC_INFO = TokenInfo(USDC, "USDC", 6)
WETH_INFO = TokenInfo(WETH, "WETH", 18)
ETH_USD = Decimal("4000")
PRICES = {WETH: ETH_USD, NATIVE: ETH_USD, USDC: Decimal("1")}

KNOWN = Recognised(
    tokens={USDC: USDC_INFO, WETH: WETH_INFO},
    wrappers={
        AWETH: Wrapper(Wrapping.SUPPLY, WETH_INFO),
        AUSDC: Wrapper(Wrapping.SUPPLY, USDC_INFO),
        DEBT_USDC: Wrapper(Wrapping.DEBT, USDC_INFO),
    },
    position_managers=frozenset(
        {BASE_DEPLOYMENT.v3_position_manager, BASE_DEPLOYMENT.v4_position_manager}
    ),
    weth=WETH,
)


def classified(rows: list[dict[str, Any]], wallet: str = WALLET) -> Judged:
    return judge(transactions(rows, wallet, KNOWN), KNOWN, PRICES)


def kinds(actions: tuple[Action, ...]) -> list[str]:
    return [f"{a.kind}:{a.detail}" if a.detail else str(a.kind) for a in actions]


# --- the classifier ----------------------------------------------------------------


def test_a_relayed_eip7702_week_is_read_in_full() -> None:
    """The two relayed actions are exactly what `/addresses/{a}/transactions`
    leaves out; here they are listed, each relayer cut as its own transfer."""
    judged = classified(base_week())

    assert kinds(judged.actions) == [
        "swap",
        "liquidity:withdrawal",
        "sent",
        "lending",
        "sent",
        "liquidity:withdrawal",
    ]
    assert judged.relayed == 2
    assert judged.hidden == Hidden(unsolicited=1)


def test_an_aave_supply_is_reported_in_the_underlying_not_the_atoken() -> None:
    """The aWETH mint (0.0310) carries accrued interest; 0.0222 ETH was supplied."""
    lending = next(a for a in classified(base_week()).actions if a.kind is Kind.LENDING)

    [part] = lending.parts
    assert (part.verb, part.amount, part.symbol) == (
        "supplied", Decimal("0.022208970712454581"), "ETH",
    )


def test_a_relayer_cut_is_a_transfer_never_a_fee() -> None:
    sent = [a for a in classified(base_week()).actions if a.kind is Kind.SENT]

    assert [(a.legs[0].counterparty, a.legs[0].amount) for a in sent] == [
        (RELAYER_FEES, Decimal("-0.009589")),
        (RELAYER_FEES, Decimal("-0.007447")),
    ]


def test_an_unverified_router_is_classified_by_its_flows() -> None:
    swap = classified(base_week()).actions[0]

    assert swap.kind is Kind.SWAP
    assert {leg.symbol: leg.amount for leg in swap.legs} == {
        "ETH": Decimal("-0.003996970724818315"), "USDC": Decimal("10.280442"),
    }


def test_gas_is_only_what_the_wallet_sent() -> None:
    """The relayed rows carry fees too: the relayer's gas, not the wallet's."""
    judged = classified(base_week())

    assert judged.gas_transactions == 2
    assert judged.gas == Decimal("0.000005415410097856")


@pytest.mark.parametrize(
    ("inner", "detail"),
    [
        ({"0x0c49ccbe", "0xfc6f7865", "0x49404b7c"}, "remove"),
        ({"0xfc6f7865", "0x49404b7c"}, "collect"),
        ({"0x219f5d17", "0x12210e8a"}, "add"),
    ],
)
def test_a_multicall_is_named_by_its_inner_calls(inner: set[str], detail: str) -> None:
    txs = transactions(base_week(), WALLET, KNOWN)
    assert needs_selectors(txs, KNOWN)

    judged = judge(txs, KNOWN, PRICES, {LP_SENT_TX: frozenset(inner)})

    assert judged.actions[-1].detail == detail


def test_a_minted_position_nft_is_an_opened_position() -> None:
    tx, at = "0x1ba03d64" + "0" * 56, "2026-06-15T20:37:00.000000Z"
    rows = [
        native(tx, WALLET, V3_MANAGER, 741827735562873106, at=at, method="multicall", fee=1),
        nft(tx, ZERO, WALLET, V3_MANAGER, "5548681", at=at),
        erc20(tx, WALLET, "0xc6962004f452be9203591991d15f6b388e09e8d0", 26_000_000, USDC_TOKEN,
              at=at),
    ]

    [action] = classified(rows).actions

    assert (action.kind, action.detail, action.token_id) == (Kind.LIQUIDITY, "open", "5548681")


def test_a_borrow_and_a_repay_are_read_from_the_debt_token() -> None:
    at = "2026-08-10T15:00:00.000000Z"
    borrow, repay = "0xb0" + "0" * 62, "0xb1" + "0" * 62
    pool = "0xa238dd80c259a72e81d7e4664a9801593f98d1c5"
    rows = [
        erc20(repay, WALLET, ZERO, 100_000_000, DEBT_USDC_TOKEN, at=at),
        erc20(repay, WALLET, AUSDC, 100_000_000, USDC_TOKEN, at=at),
        erc20(borrow, ZERO, WALLET, 250_000_000, DEBT_USDC_TOKEN, at=at),
        erc20(borrow, AUSDC, WALLET, 250_000_000, USDC_TOKEN, at=at),
        native(borrow, WALLET, pool, 0, at=at, method="borrow", fee=1),
    ]

    repaid, borrowed = classified(rows).actions

    assert [(p.verb, p.amount, p.symbol) for p in borrowed.parts] == [
        ("borrowed", Decimal("250"), "USDC"),
    ]
    assert [(p.verb, p.amount, p.symbol) for p in repaid.parts] == [
        ("repaid", Decimal("100"), "USDC"),
    ]


def test_poisoning_is_hidden_counted_and_flagged() -> None:
    """Homoglyph tokens and a zero-value transferFrom 'from' the wallet to
    lookalikes, and dust from one: none is shown, every one is counted, and
    each imitates a real counterparty."""
    judged = classified(poisoned_day(), wallet=OTHER_WALLET)

    shown = [(a.kind, a.legs[0].counterparty) for a in judged.actions]
    assert shown == [(Kind.SENT, REAL_PAYEE), (Kind.RECEIVED, ZERO_VALUE_REAL)]
    assert judged.hidden == Hidden(unsolicited=2, zero_value=1, dust=1, lookalike=4)


def test_a_lookalike_shares_both_ends_and_nothing_else_does() -> None:
    assert looks_like(LOOKALIKE_1, REAL_PAYEE)
    assert not looks_like(REAL_PAYEE, REAL_PAYEE)
    assert not looks_like(ZERO_VALUE_LOOKALIKE, REAL_PAYEE)


def test_an_unlisted_token_in_the_wallets_own_transaction_is_shown() -> None:
    """The allowlist is for what others send: a wallet's own swap into a token
    nobody listed is still its own action."""
    tx, at = "0xc0" + "0" * 62, "2026-09-18T10:00:00.000000Z"
    odd = {"address_hash": "0x" + "ab" * 20, "symbol": "ODD", "decimals": "18"}
    rows = [
        native(tx, WALLET, GATEWAY, 10**17, at=at, method="swap", fee=1),
        erc20(tx, "0x" + "cd" * 20, WALLET, 5 * 10**18, odd, at=at),
    ]

    [action] = classified(rows).actions

    assert action.kind is Kind.SWAP and action.legs[1].symbol == "ODD"


@pytest.mark.parametrize(
    ("symbol", "shown"),
    [("[click](https://evil.example)", "unlisted token"), (None, "unlisted token"),
     ("ODD", "ODD")],
)
def test_an_explorer_symbol_is_quoted_only_when_plain(symbol: str | None, shown: str) -> None:
    """Regression: a token's own symbol went verbatim into the reply, so a
    token joining the wallet's transaction could post a masked link, and a
    missing symbol printed as "None"."""
    tx, at = "0xc1" + "0" * 62, "2026-09-18T10:00:00.000000Z"
    odd = {"address_hash": "0x" + "ab" * 20, "symbol": symbol, "decimals": "18"}
    rows = [
        native(tx, WALLET, GATEWAY, 10**17, at=at, method="swap", fee=1),
        erc20(tx, "0x" + "cd" * 20, WALLET, 5 * 10**18, odd, at=at),
    ]

    text = _render(rows, private=True)

    assert f"5 {shown}" in text
    assert "evil.example" not in text and "None" not in text


def test_an_explorer_method_is_quoted_only_when_plain() -> None:
    tx, at = "0xc2" + "0" * 62, "2026-09-18T10:00:00.000000Z"
    rows = [native(tx, WALLET, GATEWAY, 0, at=at, method="a`](https://evil.example)", fee=1)]

    [action] = classified(rows).actions

    assert action.kind is Kind.OTHER and action.detail == ""


def test_the_footer_says_unlisted_tokens_appear_in_the_wallets_own_transactions() -> None:
    assert "only in the wallet's own transactions" in _render(base_week(), private=True)
    assert "transações da própria carteira" in _render(base_week(), private=True, pt=True)


# --- the explorer cursor --------------------------------------------------------------


def _item(n: int) -> dict[str, Any]:
    return {"hash": f"0x{n:064x}", "timestamp": "2026-09-18T00:00:00Z", "type": "coin_transfer"}


async def test_a_null_cursor_field_is_sent_empty_and_paging_ends() -> None:
    """Regression: a null `internal_transaction_index` left out of the next
    request returned page one again, forever. Sent as "" it pages on."""
    seen: list[httpx.QueryParams] = []

    def explorer(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params)
        cursor = request.url.params
        if "block_number" not in cursor:
            page, following = [_item(1), _item(2)], {
                "block_number": 51538002, "transaction_index": 136,
                "internal_transaction_index": None, "token_transfer_index": 789,
                "token_transfer_batch_index": None,
            }
        elif "internal_transaction_index" not in cursor:
            page, following = [_item(1), _item(2)], {"block_number": 51538002}  # the trap
        else:
            page, following = [_item(3)], None
        return httpx.Response(200, json={"items": page, "next_page_params": following})

    async with httpx.AsyncClient(transport=httpx.MockTransport(explorer)) as client:
        rows = await activity_rows(
            client, "https://base.blockscout.com", WALLET,
            datetime(2026, 9, 17, tzinfo=UTC), datetime(2026, 9, 24, tzinfo=UTC),
        )

    assert [r["hash"] for r in rows.items] == [_item(n)["hash"] for n in (1, 2, 3)]
    assert not rows.truncated
    assert seen[1]["internal_transaction_index"] == ""
    assert seen[1]["token_transfer_batch_index"] == ""
    assert seen[1]["from_address_hashes_to_include"] == WALLET
    assert seen[0]["age_from"] == "2026-09-17T00:00:00Z"


async def test_a_cursor_that_repeats_ends_the_read_as_truncated() -> None:
    def explorer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [_item(1)], "next_page_params": {"block_number": 1}}
        )

    calls = 0

    def counting(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return explorer(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(counting)) as client:
        rows = await activity_rows(
            client, "https://x", WALLET, datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 2, tzinfo=UTC),
        )

    assert rows.truncated and calls == 2


async def test_pages_stop_at_the_cap_and_say_so() -> None:
    page = iter(range(100))

    def explorer(request: httpx.Request) -> httpx.Response:
        n = next(page)
        return httpx.Response(
            200, json={"items": [_item(n)], "next_page_params": {"block_number": n}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(explorer)) as client:
        rows = await activity_rows(
            client, "https://x", WALLET, datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 2, tzinfo=UTC), max_pages=4,
        )

    assert len(rows.items) == 4 and rows.truncated


def test_every_null_becomes_an_empty_string() -> None:
    assert next_cursor({"a": None, "b": 7, "c": "x"}) == {"a": "", "b": "7", "c": "x"}


async def test_inner_selectors_come_from_the_decoded_multicall() -> None:
    decoded = {
        "method_id": "ac9650d8",
        "parameters": [{"name": "data", "value": ["0x0c49ccbe" + "00" * 32, "0xfc6f7865"]}],
    }

    def explorer(request: httpx.Request) -> httpx.Response:
        assert request.url.params["filter"] == "from"
        items = [{"hash": LP_SENT_TX.upper(), "decoded_input": decoded}, {"hash": "0x1"}]
        return httpx.Response(200, json={"items": items})

    async with httpx.AsyncClient(transport=httpx.MockTransport(explorer)) as client:
        found = await multicall_selectors(client, "https://x", WALLET)

    assert found == {LP_SENT_TX.lower(): frozenset({"0x0c49ccbe", "0xfc6f7865"})}


# --- the window -----------------------------------------------------------------------

NOW = datetime(2026, 9, 24, 14, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("question", "start", "end", "named"),
    [
        ("show my wallet activity", datetime(2026, 9, 17, tzinfo=UTC), NOW, False),
        ("o que essa carteira fez essa semana?", datetime(2026, 9, 17, tzinfo=UTC), NOW, True),
        ("what did this wallet do this week?", datetime(2026, 9, 17, tzinfo=UTC), NOW, True),
        ("minhas transações de ontem", datetime(2026, 9, 23, tzinfo=UTC),
         datetime(2026, 9, 24, tzinfo=UTC), True),
        ("o que minha carteira fez nos últimos 30 dias?", datetime(2026, 8, 25, tzinfo=UTC),
         NOW, True),
        ("my wallet transactions today", datetime(2026, 9, 24, tzinfo=UTC), NOW, True),
    ],
)
def test_the_window_is_the_questions(
    question: str, start: datetime, end: datetime, named: bool
) -> None:
    window = activity_window(question, NOW)
    assert (window.start, window.end, window.named, window.clamped) == (start, end, named, False)


def test_a_window_wider_than_a_month_is_cut_and_says_so() -> None:
    window = activity_window("my wallet activity in the last 90 days", NOW)

    assert window.clamped
    assert window.end - window.start == timedelta(days=MAX_DAYS)


def test_a_window_never_runs_past_now() -> None:
    assert activity_window("what did my wallet do this month?", NOW).end == NOW


# --- the answer -----------------------------------------------------------------------

WINDOW = ActivityWindow(datetime(2026, 9, 17, tzinfo=UTC), NOW, named=True)
BASE_KNOWN = {c.key: KNOWN for c in (ETHEREUM, BASE, ARBITRUM)}
FULL_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def _chains(rows: list[dict[str, Any]], wallet: str = WALLET) -> list[ChainActivity]:
    judged = classified(rows, wallet)
    base = ChainActivity(
        BASE,
        actions=judged.actions,
        gas=judged.gas,
        gas_transactions=judged.gas_transactions,
        relayed=judged.relayed,
        hidden=judged.hidden,
        prices=PRICES,
    )
    return [ChainActivity(ETHEREUM), base, ChainActivity(ARBITRUM, unreachable="timed out")]


def _render(
    rows: list[dict[str, Any]], *, private: bool, pt: bool = False, wallet: str = WALLET
) -> str:
    language = Language.PORTUGUESE if pt else Language.ENGLISH
    return render_activity(wallet, WINDOW, _chains(rows, wallet), BASE_KNOWN, language,
                           private=private)


def test_a_dm_names_every_counterparty_in_full() -> None:
    text = _render(base_week(), private=True)

    assert f"`{RELAYER_FEES}`" in text
    assert "swapped 0.00399697 ETH → 10.2804 USDC (≈ $15.99)" in text
    assert "Aave: supplied 0.0222090 ETH (≈ $88.84)" in text
    assert "Uniswap: LP withdrawal (liquidity and/or fees) — +10.5027 USDC" in text
    assert "_Period: 17 Sep 2026 00:00 – 24 Sep 2026 14:05 UTC._" in text
    assert "**Base** — 6 actions" in text
    assert "; 2 more submitted by a relayer" in text
    assert "**Arbitrum** — could not be read (timed out)" in text
    assert "**Ethereum** — no activity" in text
    for address in FULL_ADDRESS.findall(text):
        assert len(address) == 42, "an address is never shortened in a DM"


def test_a_channel_redacts_counterparties_and_the_wallet() -> None:
    text = _render(base_week(), private=False)

    assert "sent 0.009589 USDC (≈ $0.01) to an external address" in text
    assert "…3d68" in text.splitlines()[0]
    assert not FULL_ADDRESS.search(text), "no full address is posted to a channel"


def test_a_channel_in_portuguese_says_um_endereco_externo() -> None:
    text = _render(base_week(), private=False, pt=True)

    assert "para um endereço externo" in text
    assert "_Período: 17/09/2026 00:00 – 24/09/2026 14:05 UTC._" in text
    assert "**Arbitrum** — não foi possível ler (tempo esgotado)" in text


def test_hidden_lookalikes_are_a_warning_line() -> None:
    text = _render(poisoned_day(), private=True, wallet=OTHER_WALLET)

    assert "Hidden: 2 unsolicited token transfer(s), 1 zero-value transfer(s), 1 dust" in text
    assert "⚠️ 4 of the hidden transfers used an address imitating one" in text
    assert LOOKALIKE_1 not in text, "a lookalike is never printed, even in a DM"
    assert f"`{REAL_PAYEE}`" in text


def test_long_weeks_are_bounded_with_a_count_of_the_rest() -> None:
    at = "2026-09-18T10:{:02d}:00.000000Z"
    rows = [
        erc20(f"0x{n:064x}", WALLET, REAL_PAYEE, 1_000_000, USDC_TOKEN, at=at.format(n),
              receiver_contract=False)
        for n in range(MAX_LINES + 3)
    ]

    text = _render(rows, private=True)

    assert "…and 3 more." in text
    assert text.count(f"`{REAL_PAYEE}`") == MAX_LINES


def test_a_stale_explorer_is_reported_not_read_as_no_activity() -> None:
    """Regression, from production: Arbitrum's explorer stopped at 2026-09-22
    21:41 UTC while reporting "finished indexing", and a position minted the
    next day read as "nenhuma atividade"."""
    from datetime import UTC, datetime

    from chatmemory.adapters.chain.activity import ChainActivity
    from chatmemory.adapters.chain.activity_explorer import Rows
    from chatmemory.adapters.chain.activity_reader import _with_freshness
    from chatmemory.adapters.chain.activity_render import render_activity
    from chatmemory.adapters.chain.tokens import ARBITRUM
    from chatmemory.app.language import Language
    from chatmemory.app.wallet_activity import ActivityWindow

    end = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
    window = ActivityWindow(start=datetime(2026, 9, 17, tzinfo=UTC), end=end, named=False)
    stale = Rows((), indexed_until=datetime(2026, 9, 22, 21, 41, tzinfo=UTC))
    fresh = Rows((), indexed_until=end)

    marked = _with_freshness(ChainActivity(ARBITRUM), stale, window)
    assert marked.stale_until == stale.indexed_until
    assert _with_freshness(ChainActivity(ARBITRUM), fresh, window).stale_until is None

    text = render_activity(
        "0xb26b933a075fbb3d4e8b0925cad4f2bc345475e0", window, [marked], {}, Language.PORTUGUESE,
        private=True,
    )
    assert "o explorador só indexou até 22/09/2026 21:41 UTC" in text
    assert "nenhuma atividade" not in text
