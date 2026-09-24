"""Wallet balances: whose address may leave, and what a failure looks like.

The property this feature lives or dies by is the first one. Rooting means the
assistant can look up an address **the asker typed** and cannot look up one it
read in a channel -- without which anyone able to ask it a question could use
it to sweep every address mentioned across the channels it can read, which is
more channels than most individuals can.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from chatmemory.adapters.chain.provider import WALLET_TOOL, PricedAsset, WalletProvider
from chatmemory.adapters.chain.registration import ChainToolsConfig, build_chain_tools
from chatmemory.adapters.chain.rpc import ChainReader
from chatmemory.adapters.chain.tokens import BASE, ETHEREUM, tokens_for
from chatmemory.adapters.web.limits import CallBudget
from chatmemory.app.egress import (
    AuthorizedQuery,
    EgressGuard,
    EgressRefused,
    EgressRequest,
    ProvenancedQuery,
    QueryOrigin,
    authorized,
    keep_asker_words,
)
from chatmemory.domain.chain import find_addresses, is_address, normalise
from chatmemory.domain.identity import PersonRef

ADDRESS = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
OTHER = "0x00000000219ab540356cBB839Cbe05303d7705Fa"
ASKER = PersonRef(platform="discord", platform_user_id=7)


# --- the rule about whose words an address may be -----------------------


def test_an_address_the_asker_typed_survives_rooting() -> None:
    question = f"what does {ADDRESS} hold on base?"
    assert keep_asker_words(ADDRESS, question) == ADDRESS


def test_an_address_the_asker_did_not_type_is_stripped_to_nothing() -> None:
    """The whole feature rests on this. An address that appears in a message
    rather than in the question must not become a lookup."""
    question = f"what does {ADDRESS} hold on base?"
    assert keep_asker_words(OTHER, question) == ""


def test_the_guard_refuses_an_address_from_retrieved_content() -> None:
    guard = EgressGuard()
    request = EgressRequest(
        asker=ASKER,
        query=ProvenancedQuery(
            text=OTHER,
            origin=QueryOrigin.RETRIEVED_CONTENT,
            question=f"what does {ADDRESS} hold?",
        ),
        provider=WalletProvider.server,
    )
    with pytest.raises(EgressRefused):
        guard.authorize(request)


def test_the_wallet_provider_is_not_held_to_a_closed_vocabulary() -> None:
    """Membership is impossible for a set of size 2^160; rooting is the rule.

    Asserted rather than assumed: were the server name ever added to that
    table, every real lookup would be refused as a non-member, and the
    failure would look like the tool simply never working."""
    from chatmemory.app.egress import closed_vocabulary_for

    assert closed_vocabulary_for(WalletProvider.server) is None


# --- the structural gate after rooting ----------------------------------


@pytest.mark.parametrize("text", [ADDRESS, ADDRESS.lower(), f"  {ADDRESS}  "])
def test_well_formed_addresses_are_accepted(text: str) -> None:
    assert is_address(text)


@pytest.mark.parametrize(
    "text",
    [
        "balance",
        "base",
        "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA960",  # one short
        "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045ff",  # one long
        f"{ADDRESS}extra",
        f"prefix{ADDRESS}",
        "0xZZZZ6BF26964aF9D7eEd9e03E53415D37aA96045",
        "",
    ],
)
def test_anything_that_is_not_an_address_is_rejected(text: str) -> None:
    assert not is_address(text)


def test_an_address_is_lowercased_before_it_is_sent() -> None:
    """EIP-55 mixes case as a checksum and rooting compares case-folded, so a
    model may echo a different case than the asker typed."""
    assert normalise(ADDRESS) == ADDRESS.lower()


def test_addresses_are_found_in_order_without_duplicates() -> None:
    text = f"{ADDRESS} then {OTHER} then {ADDRESS.lower()}"
    assert find_addresses(text) == (ADDRESS.lower(), OTHER.lower())


# --- reading a chain ----------------------------------------------------


def _rpc(results: dict[int, str]) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        import json

        batch = json.loads(request.content)
        return httpx.Response(
            200,
            json=[
                {"jsonrpc": "2.0", "id": c["id"], "result": results.get(c["id"], "0x0")}
                for c in batch
            ],
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_a_chain_reports_its_native_and_token_balances() -> None:
    reader = ChainReader(
        ETHEREUM,
        "key",
        client=_rpc({0: hex(10**18), 1: hex(5 * 10**6)}),
    )
    result = await reader.balances(ADDRESS, tokens_for(ETHEREUM))

    assert result.ok
    assert result.native == Decimal(1)
    assert [(h.symbol, h.amount) for h in result.tokens] == [("USDC", Decimal(5))]


async def test_a_zero_token_balance_is_not_listed() -> None:
    reader = ChainReader(ETHEREUM, "key", client=_rpc({0: hex(10**18)}))
    result = await reader.balances(ADDRESS, tokens_for(ETHEREUM))
    assert result.tokens == ()


async def test_an_unreachable_chain_is_reported_not_rendered_as_empty() -> None:
    """ "Could not be read" and "holds nothing" are indistinguishable once they
    are prose, and only one of them is a reason to worry."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    reader = ChainReader(
        ETHEREUM, "key", client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )
    result = await reader.balances(ADDRESS, tokens_for(ETHEREUM))

    assert not result.ok
    assert result.unreachable
    assert result.native is None


async def test_the_endpoint_key_never_reaches_a_log_message() -> None:
    reader = ChainReader(ETHEREUM, "s3cret-key")
    assert "s3cret-key" not in reader.redact("failed for https://x/v3/s3cret-key")


# --- the guarded provider ----------------------------------------------


def _provider(client: httpx.AsyncClient, prices: object | None = None) -> WalletProvider:
    return WalletProvider(
        [ChainReader(c, "key", client=client) for c in (ETHEREUM, BASE)],
        CallBudget(3),
        prices=prices,  # type: ignore[arg-type]
    )


def _clearance(text: str, question: str) -> AuthorizedQuery:
    return EgressGuard().authorize(
        EgressRequest(
            asker=ASKER,
            query=ProvenancedQuery(text=text, origin=QueryOrigin.ASKER, question=question),
            provider=WalletProvider.server,
        )
    )


async def test_a_cleared_address_is_looked_up() -> None:
    provider = _provider(_rpc({0: hex(2 * 10**18)}))
    question = f"what does {ADDRESS} hold?"
    with authorized(_clearance(ADDRESS, question)):
        result = await provider.call_tool(WALLET_TOOL, {"address": ADDRESS})

    assert not result.is_error
    assert "Ethereum" in result.text and "Base" in result.text


async def test_a_rooted_word_that_is_not_an_address_is_refused() -> None:
    """Rooting admits any word the asker wrote; the gate is what stops the
    rest reaching an endpoint."""
    provider = _provider(_rpc({}))
    with authorized(_clearance("balance", "what is my balance")):
        result = await provider.call_tool(WALLET_TOOL, {"address": "balance"})

    assert result.is_error
    assert "not_an_address" in result.text


async def test_without_a_clearance_nothing_is_looked_up() -> None:
    """A provider reached any other way finds no clearance and refuses, which
    is what makes the boundary unskippable rather than documented."""
    provider = _provider(_rpc({0: hex(10**18)}))
    result = await provider.call_tool(WALLET_TOOL, {"address": ADDRESS})
    assert result.is_error
    assert "egress_refused" in result.text


async def test_an_unknown_tool_is_refused() -> None:
    provider = _provider(_rpc({}))
    result = await provider.call_tool("transfer", {"address": ADDRESS})
    assert result.is_error


async def test_the_provider_offers_exactly_one_read_only_tool() -> None:
    provider = _provider(_rpc({}))
    tools = await provider.list_tools()
    assert [t.name for t in tools] == [WALLET_TOOL]
    assert not tools[0].effect.mutates


async def test_one_chain_failing_still_reports_the_other() -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if "base-mainnet" in str(request.url):
            raise httpx.ConnectError("refused", request=request)
        import json

        batch = json.loads(request.content)
        return httpx.Response(
            200,
            json=[
                {"jsonrpc": "2.0", "id": c["id"], "result": hex(10**18) if c["id"] == 0 else "0x0"}
                for c in batch
            ],
        )

    provider = _provider(httpx.AsyncClient(transport=httpx.MockTransport(flaky)))
    question = f"what does {ADDRESS} hold?"
    with authorized(_clearance(ADDRESS, question)):
        result = await provider.call_tool(WALLET_TOOL, {"address": ADDRESS})

    assert not result.is_error
    assert "could not be read" in result.text
    assert "1.000000 ETH" in result.text


async def test_usd_values_use_the_market_price_and_state_its_age() -> None:
    class Prices:
        async def usd_price(self, symbol: str) -> PricedAsset | None:
            return PricedAsset(symbol, Decimal(2000), "2026-09-18T10:00Z")

    provider = _provider(_rpc({0: hex(10**18)}), prices=Prices())
    question = f"what does {ADDRESS} hold?"
    with authorized(_clearance(ADDRESS, question)):
        result = await provider.call_tool(WALLET_TOOL, {"address": ADDRESS})

    assert "$2,000.00" in result.text
    assert "2026-09-18T10:00Z" in result.text


async def test_the_answer_names_the_limit_of_the_token_set() -> None:
    """A token nobody listed is invisible to this lookup, not absent, and an
    answer that quietly omitted it would be wrong in the direction people
    care about."""
    provider = _provider(_rpc({0: hex(10**18)}))
    question = f"what does {ADDRESS} hold?"
    with authorized(_clearance(ADDRESS, question)):
        result = await provider.call_tool(WALLET_TOOL, {"address": ADDRESS})

    assert "not visible to this lookup" in result.text


# --- registration -------------------------------------------------------


def test_no_key_registers_nothing() -> None:
    """Not registered rather than registered-and-failing: a tool the model can
    see but cannot use is one it will plan around and then fail on."""
    assert build_chain_tools().servers == ()


def test_a_key_registers_only_read_only_tools() -> None:
    tools = build_chain_tools(ChainToolsConfig(infura_key="k"))
    assert tools.server_names == (WalletProvider.server, "defi_positions")
    assert {e.tool for e in tools.allowlist} == {
        "wallet_balances", "liquidity_positions", "lending_positions", "defi_positions",
        "portfolio_summary", "wallet_activity",
    }
    for entry in tools.allowlist:
        assert entry.effect.mutates is False
        assert entry.mutation_enabled is False


# --- the route that makes the tool reachable ----------------------------


@pytest.mark.parametrize(
    "question",
    [
        # The verbatim production message. It was answered from a channel
        # message about a colleague's crypto project, because the corpus
        # answered first and the critic found it sufficient -- so the run
        # never escalated to the tool that could actually answer.
        "0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5",
        "qual o saldo da carteira 0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5?",
        "what does 0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5 hold on base",
        "quanto tem na wallet 0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5",
    ],
)
def test_a_question_naming_an_address_goes_to_the_chain(question: str) -> None:
    """A balance is never in the corpus. A channel message about a wallet is a
    record of what somebody said, and answering from one reports a colleague's
    project summary as somebody's balance."""
    from chatmemory.app.routing import wallet_question

    asked = wallet_question(question)
    assert asked is not None
    assert asked.address == "0xd5c95af87f6e1e83507ac96b2ee4484b9afebdd5"


def test_a_wallet_question_with_no_address_asks_for_one() -> None:
    """"qual o balanco da carteira?" searched the corpus and answered from
    whatever a colleague had written about wallets. No channel holds a live
    balance, so there is nothing there to find."""
    from chatmemory.app.routing import wallet_question

    asked = wallet_question("qual o balanco da carteira?")
    assert asked is not None
    assert asked.address is None


@pytest.mark.parametrize(
    "question",
    [
        # The address is the SUBJECT of a question about the conversation.
        "what did people say about 0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5",
        "o que o pessoal falou sobre a carteira 0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5",
        "quem mencionou 0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5",
        # Nothing to do with wallets at all.
        "what was decided about the deploy last week",
        "what did people ask me to do today",
        "qual o valor do Ethereum ?",
    ],
)
def test_questions_about_the_conversation_still_reach_the_corpus(question: str) -> None:
    """The bound in the other direction: an address can be what a question is
    *about*, and that question belongs to the corpus."""
    from chatmemory.app.routing import wallet_question

    assert wallet_question(question) is None
