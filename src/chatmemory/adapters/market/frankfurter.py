"""Currency conversion from Frankfurter's ECB reference rates.

Two things about this source shape the code.

The rates are published once per business day. They are daily reference
rates, and every result says so with the rate's date; a reader must not come
away believing they could exchange money at that rate now.

The amount never leaves. Frankfurter can convert an amount itself, but a
number sent to it is a number the egress guard never checked -- the closed
vocabulary covers text -- and an "amount" is as good a carrier for digits as
any. So only the two currency codes are sent, the rate comes back, and the
multiplication happens here. It also lets one cached rate serve every amount.

The endpoint is `api.frankfurter.dev/v1`. The old `api.frankfurter.app` host
answers with a 301, and redirects are not followed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from types import MappingProxyType

import httpx

from chatmemory.adapters.market.arguments import (
    ArgumentCheck,
    ArgumentRefusal,
    Lookup,
    amount,
    member,
    only,
    refused,
)
from chatmemory.adapters.market.cache import FreshCache
from chatmemory.adapters.market.provider import (
    DEFAULT_TIMEOUT,
    MarketProvider,
    MarketToolSpec,
    ProviderUnavailable,
    UnsupportedBySource,
    as_decimal,
    read_json,
)
from chatmemory.adapters.market.quotes import Quote, Timing, figure, money
from chatmemory.adapters.web.limits import CallBudget, RateLimiter
from chatmemory.adapters.web.results import as_mapping, as_text
from chatmemory.app.egress import ISO_4217_CODES, MARKET_FX_PROVIDER

FRANKFURTER_ENDPOINT = "https://api.frankfurter.dev/v1"
FRANKFURTER_LABEL = "Frankfurter (ECB reference rates)"
FRANKFURTER_PAGE = "https://frankfurter.dev"
DEFAULT_TTL_SECONDS = 300.0

CONVERT = "convert"
ARG_AMOUNT = "amount"
ARG_FROM = "from"
ARG_TO = "to"
_ARGUMENTS = frozenset({ARG_AMOUNT, ARG_FROM, ARG_TO})

_UNSUPPORTED_STATUSES = frozenset({404, 422})
"""What Frankfurter answers for a valid ISO code it does not publish."""

_CODE = {
    "type": "string",
    "pattern": "^[A-Z]{3}$",
    "description": "An ISO 4217 currency code such as USD, EUR or BRL. Nothing else is accepted.",
}

TOOL = MarketToolSpec(
    name=CONVERT,
    description=(
        "Convert an amount of money from one currency to another, for example "
        "dollars to reais or euros, using the daily exchange rate reference "
        "published by the European Central Bank. Gives the converted amount, "
        "the rate used and the rate's date."
    ),
    input_schema=MappingProxyType(
        {
            "type": "object",
            "properties": {
                ARG_AMOUNT: {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "The amount to convert, as a number.",
                },
                ARG_FROM: _CODE,
                ARG_TO: _CODE,
            },
            "required": sorted(_ARGUMENTS),
            "additionalProperties": False,
        }
    ),
)


def check_conversion(arguments: Mapping[str, object]) -> ArgumentCheck:
    """Two ISO 4217 codes and a positive amount, or a refusal."""
    shape = only(arguments, _ARGUMENTS)
    if shape is not None:
        return refused(shape)
    source = member(arguments[ARG_FROM], ISO_4217_CODES)
    target = member(arguments[ARG_TO], ISO_4217_CODES)
    if source is None or target is None:
        return refused(ArgumentRefusal.NOT_A_MEMBER)
    if source == target:
        return refused(ArgumentRefusal.SAME_CURRENCY)
    value = amount(arguments[ARG_AMOUNT])
    if value is None:
        return refused(ArgumentRefusal.INVALID_AMOUNT)
    return ArgumentCheck(lookup=Lookup(terms=(source, target), amount=value))


class FrankfurterProvider(MarketProvider):
    """`ToolSession` over Frankfurter's latest reference rates."""

    def __init__(
        self,
        budget: CallBudget,
        *,
        endpoint: str = FRANKFURTER_ENDPOINT,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        limiter: RateLimiter | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            server=MARKET_FX_PROVIDER,
            label=FRANKFURTER_LABEL,
            endpoint=endpoint.rstrip("/"),
            tool=TOOL,
            budget=budget,
            cache=FreshCache(ttl_seconds),
            limiter=limiter,
            timeout_seconds=timeout_seconds,
            client=client,
            transport=transport,
        )

    def check_arguments(self, arguments: Mapping[str, object]) -> ArgumentCheck:
        return check_conversion(arguments)

    async def fetch(self, lookup: Lookup, client: httpx.AsyncClient) -> Quote:
        source, target = lookup.terms
        response = await client.get(
            f"{self.endpoint}/latest",
            # The two codes. Not the amount -- see the module docstring.
            params={"from": source, "to": target},
            timeout=self._timeout,
        )
        if response.status_code in _UNSUPPORTED_STATUSES:
            raise UnsupportedBySource(f"frankfurter does not publish {source}/{target}")
        payload = read_json(response, FRANKFURTER_LABEL)
        rate = as_decimal(as_mapping(payload.get("rates")).get(target))
        if rate is None or as_text(payload.get("base")) != source:
            raise ProviderUnavailable("frankfurter returned no usable rate")
        return Quote(
            instrument=f"{source}/{target}",
            value=rate,
            unit=target,
            source=FRANKFURTER_LABEL,
            url=FRANKFURTER_PAGE,
            timing=Timing.REFERENCE_DATE,
            as_of=_published(payload.get("date")),
        )

    def describe(self, quote: Quote, lookup: Lookup) -> str:
        source, target = lookup.terms
        # Always present for this tool: `check_conversion` refuses without it.
        given = lookup.amount if lookup.amount is not None else Decimal(0)
        converted = given * quote.value
        return (
            f"{figure(given)} {source} = {money(converted)} {target} "
            f"(rate used: 1 {source} = {figure(quote.value)} {target}, "
            "daily reference rate)"
        )


def _published(value: object) -> date:
    try:
        return date.fromisoformat(as_text(value))
    except ValueError as exc:
        # Undated, the rate cannot be stated honestly, so it is not stated.
        raise ProviderUnavailable("frankfurter returned a rate without a valid date") from exc
