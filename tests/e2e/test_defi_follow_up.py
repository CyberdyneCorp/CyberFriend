"""S5: a positions follow-up recalls the address its first turn stored.

The production failure: "what positions does 0x... have" was answered from the
chain, but the turn was never remembered -- the positions provider's citation
did not pass memory's provenance check -- so "show me the v4 one" arrived with
no address and was answered from a colleague's message about their project.
The unit regression built the citation by hand, and so proved the constant was
accepted rather than that the real provider produces it.

Here nothing is seeded: turn one goes through the real provider over the fake
node, and the row it leaves in `conversation_turn` is read by SQL.
"""

from __future__ import annotations

from tests.e2e.conftest import COLLEAGUE_CRYPTO
from tests.e2e.harness.conversation import E2EBot

ADDRESS = "0xB26B933a075fBB3D4E8b0925CAd4f2bc345475e0"
COLLEAGUES_ADDRESS = "0xD5C95aF87F6e1E83507AC96b2eE4484B9AFEbDd5"


def _reached(bot: E2EBot, since: int, address: str) -> bool:
    """Whether a chain read after `since` was about `address`."""
    needle = address.lower()[2:]
    explorer = any(
        r.url.params.get("holder_address_hash", "").lower() == address.lower()
        for r in bot.web.calls[since:]
    )
    return explorer or any(needle in str(c).lower() for c in bot.web.rpc_calls(since))


async def test_a_positions_follow_up_uses_the_turn_the_first_one_stored(bot: E2EBot) -> None:
    leo = bot.person("Leo")
    dm = bot.dm(leo)

    first = await dm.say(f"Que posicoes no Uniswap temos nesse endereco : {ADDRESS}")

    assert first.edge() == "CHAIN"
    assert not first.searched
    assert _reached(bot, 0, ADDRESS)
    stored = await dm.memory_turns()
    assert len(stored) == 1, "the chain turn was not remembered"
    assert ADDRESS in stored[0].question

    since = len(bot.web.calls)
    second = await dm.say("Show me details of the Uniswap v4 open position")

    assert second.edge() == "CHAIN"
    assert not second.searched, "a positions follow-up must never search the corpus"
    assert _reached(bot, since, ADDRESS), "the follow-up did not carry the stored address"
    assert not _reached(bot, since, COLLEAGUES_ADDRESS)
    assert COLLEAGUE_CRYPTO not in second.text
    assert COLLEAGUES_ADDRESS.lower() not in second.text.lower()
    assert len(await dm.memory_turns()) == 2
