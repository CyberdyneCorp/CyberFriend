"""The stable name of what a run was for, decided where the route is decided.

A trace is named by its feature, and usage is counted by it, so the name is
set on the domain side -- by the router and the answer services -- and never
inferred from strings in an exporter. The identifiers are part of what the
trace store holds and what usage reports group by: renaming one splits its
history in two, so a new feature adds a constant rather than changing one.
"""

from __future__ import annotations

CORPUS_FIXED = "corpus.fixed"
"""Answered from the team's conversations by the single-lookup path."""
CORPUS_LOOP = "corpus.loop"
"""Answered from the team's conversations by the planning loop."""
CORPUS_CATCHUP = "corpus.catchup"
"""A summary of a period of one channel ("what did I miss in #x")."""
CORPUS_SAID_BY = "corpus.said_by"
"""What one person said, from their own messages ("what did Ana say about X")."""
MARKET_PRICE = "market.price"
"""A current crypto or index price, from market data."""
MARKET_OTHER = "market.other"
"""Any other current market figure (a currency conversion), from market data."""
WALLET_BALANCE = "wallet.balance"
WALLET_ACTIVITY = "wallet.activity"
PORTFOLIO = "portfolio"
DEFI_POSITIONS = "defi.positions"
"""Liquidity or lending positions of a wallet."""
WEB_SEARCH = "web.search"
"""An explicit request to search outside the team's conversations."""
TIME = "time"
OBLIGATIONS = "obligations"
DECISIONS = "decisions"
CAPABILITIES = "capabilities"
FEDERATION = "federation"
"""A corpus question answered by escalating to federated tools, or a request
to change which MCP servers the assistant reaches."""

FEATURES = frozenset(
    {
        CORPUS_FIXED,
        CORPUS_LOOP,
        CORPUS_CATCHUP,
        CORPUS_SAID_BY,
        MARKET_PRICE,
        MARKET_OTHER,
        WALLET_BALANCE,
        WALLET_ACTIVITY,
        PORTFOLIO,
        DEFI_POSITIONS,
        WEB_SEARCH,
        TIME,
        OBLIGATIONS,
        DECISIONS,
        CAPABILITIES,
        FEDERATION,
    }
)
"""Every feature a run can be named by. A test holds every route to one."""
