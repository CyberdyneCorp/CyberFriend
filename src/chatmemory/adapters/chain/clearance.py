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

from dataclasses import dataclass

import structlog

from chatmemory.adapters.mcp_client.session import ToolResult
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.adapters.web.query import check_query
from chatmemory.app.egress import EgressRefused, current_authorization
from chatmemory.domain.chain import is_address, normalise

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class Cleared:
    address: str


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

    if not is_address(check.query):
        log.warning("chain.not_an_address", tool=tool)
        return refusal(server, tool, "not_an_address")

    address = normalise(check.query)
    key = f"{tool}:{address}" if per_tool_budget else address
    if not budget.spend(key):
        log.warning("chain.budget_exhausted", tool=tool)
        return refusal(server, tool, "calls_per_run_exhausted")
    if not await limiter.acquire():
        log.warning("chain.rate_limited", tool=tool)
        return refusal(server, tool, "rate_limited")
    return Cleared(address)
