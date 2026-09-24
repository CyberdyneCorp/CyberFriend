"""The address check every chain tool runs before it reaches the network.

Shared rather than copied because it is the security boundary of the whole
package, and two copies are how one of them drifts. See `provider.py` for why
each step exists; in order, all before any request:

1. the clearance exists and is for this provider;
2. the cleared text is rooted in the asker's own words (or saved facts);
3. the cleared text is an address -- refused, never trimmed;
4. the per-question budget and the rate limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from chatmemory.adapters.mcp_client.session import ToolResult
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.adapters.web.query import check_query
from chatmemory.app.egress import EgressRefused, current_authorization
from chatmemory.domain.chain import is_address, normalise
from chatmemory.ports.facts import MAX_WALLETS_PER_KIND

log = structlog.get_logger()


MAX_ADDRESSES = MAX_WALLETS_PER_KIND
"""Wallets one portfolio call may cover: every wallet a person may save. More
than that is a sweep, not a question about one's own money -- and five
addresses is also what the 256-character query bound holds."""

_SEPARATORS = re.compile(r"[\s,;]+")


@dataclass(frozen=True, slots=True)
class Cleared:
    addresses: tuple[str, ...]
    #: The asker's own question, as the clearance carries it: what the answer's
    #: language is taken from. Never the model's arguments.
    question: str = ""
    #: Whether only the asker reads the answer, from the clearance's audience.
    #: False -- the channel's rule -- unless the guard was told otherwise.
    private: bool = False

    @property
    def address(self) -> str:
        return self.addresses[0]


def refusal(server: str, tool: str, reason: str) -> ToolResult:
    # An error result, not an empty one: "the lookup did not run" and "this
    # address holds nothing" must never look the same to the loop.
    return ToolResult(text=f"{server}:{tool} did not run ({reason})", is_error=True)


async def clear_address(
    server: str,
    tool: str,
    budget: CallBudget,
    limiter: RateLimiter,
    *,
    per_tool_budget: bool = False,
) -> Cleared | ToolResult:
    """The address this call may look up, or the refusal to return instead.

    `per_tool_budget` charges the budget per (tool, address) rather than per
    address, so asking for pools and then loans about one wallet are two
    questions, not one question asked twice.
    """
    rooted = _rooted(server, tool)
    if isinstance(rooted, ToolResult):
        return rooted
    text, question, private = rooted
    if not is_address(text):
        log.warning("chain.not_an_address", tool=tool)
        return refusal(server, tool, "not_an_address")
    address = normalise(text)
    key = f"{tool}:{address}" if per_tool_budget else address
    cleared = Cleared((address,), question, private)
    return await _admit(server, tool, budget, limiter, key, cleared)


async def clear_addresses(
    server: str,
    tool: str,
    budget: CallBudget,
    limiter: RateLimiter,
    *,
    limit: int = MAX_ADDRESSES,
) -> Cleared | ToolResult:
    """One or more addresses, separated by spaces or commas, each checked whole.

    Every piece must be an address: a text with one address and one word is
    refused, not trimmed to the address, for the same reason a single address
    is never trimmed. The budget is charged per (tool, set of addresses).
    """
    rooted = _rooted(server, tool)
    if isinstance(rooted, ToolResult):
        return rooted
    text, question, private = rooted
    pieces = [p for p in _SEPARATORS.split(text.strip()) if p]
    if not pieces or not all(is_address(p) for p in pieces):
        log.warning("chain.not_an_address", tool=tool)
        return refusal(server, tool, "not_an_address")
    addresses = tuple(dict.fromkeys(normalise(p) for p in pieces))
    if len(addresses) > limit:
        log.warning("chain.too_many_addresses", tool=tool, count=len(addresses))
        return refusal(server, tool, "too_many_addresses")
    key = f"{tool}:{','.join(sorted(addresses))}"
    return await _admit(server, tool, budget, limiter, key, Cleared(addresses, question, private))


def _rooted(server: str, tool: str) -> tuple[str, str, bool] | ToolResult:
    """The cleared text, the asker's question and its privacy, or the refusal."""
    # The clearance, never the arguments: those are model output.
    try:
        clearance = current_authorization(server)
    except EgressRefused as refused:
        log.warning("chain.egress_refused", tool=tool, reason=str(refused.reason))
        return refusal(server, tool, "egress_refused")

    check = check_query(clearance.question, clearance.text)
    if not check.ok:
        log.warning(
            "chain.query_refused",
            tool=tool,
            reason=str(check.refusal),
            # For an operator, never returned: these may be words a crafted
            # message put there.
            foreign=list(check.foreign),
        )
        return refusal(server, tool, str(check.refusal))
    return check.query, clearance.question, clearance.private


async def _admit(
    server: str,
    tool: str,
    budget: CallBudget,
    limiter: RateLimiter,
    key: str,
    cleared: Cleared,
) -> Cleared | ToolResult:
    """The per-question budget, then the rate limit."""
    if not budget.spend(key):
        log.warning("chain.budget_exhausted", tool=tool)
        return refusal(server, tool, "calls_per_run_exhausted")
    if not await limiter.acquire():
        log.warning("chain.rate_limited", tool=tool)
        return refusal(server, tool, "rate_limited")
    return cleared
