## Context

The fact store is a closed set of kinds, each validated on construction
(`ports.facts`), with a CHECK constraint in the database naming the same kinds.
Market answers are shown verbatim from the tool result; positions, portfolio,
activity and alerts are rendered in code. The asker's facts reach the tools of
one answer through a context variable (`app.asker.answering_with_facts`).

## Decisions

### The vocabulary is the rate source's

`domain.currency` lists exactly the currencies Frankfurter publishes (the ECB
set: AUD, BRL, CAD, CHF, CNY, CZK, DKK, EUR, GBP, HKD, HUF, IDR, ILS, INR, ISK,
JPY, KRW, MXN, MYR, NOK, NZD, PHP, PLN, RON, SEK, SGD, THB, TRY, USD, ZAR). A
preference that is accepted can always be converted, and the code that leaves
is always a member of the set -- the value stored is the code, not the words
the person typed. Names are aliases to codes, accents optional; a handful of
unsupported names (Argentine, Chilean, Colombian pesos, crypto) are recognised
only so the loose phrasings reach the refusal.

### Recognition

Strict statements ("minha moeda é X", "my currency is X", "moeda preferida:
X", "change my currency to X") take any value and are validated, so an unknown
currency gets the supported list. Loose ones ("uso X", "prefiro ver em X", "I
prefer prices in X") match only when X is a currency name, and start a clause
of an introduction only then.

### Converted in code, once per answer

`app.currency` holds the formatting (`money`), the `Conversion` value (code,
rate, reader's language) and `UsdRates`, the port. `adapters.market.usd_rates`
implements it over `FrankfurterProvider.reference_rate`, which sends the same
two-code request a conversion tool call sends and shares that provider's cache
and client; a ten-minute cache on top serves every figure in an answer from one
read. Each tool reads the asker's preference from the answer's facts, never
from model arguments, and renders the second figure itself. With the market
tools off, a provider of the same class and host is built for the rates and
never registered as a tool.

The rate lookup is not cleared by the egress guard: there is no question to
root it in. It is declared here instead, as the price alerts' constant
CoinGecko request was declared in `add-alert-kinds`.

### Alerts

A sweep has no asker. `PreferredCurrencies` reads the alert owner's stored
preference from the fact store when a price or health alert fires. A range
alert has no dollar figure. The level a price alert was set at stays in
dollars.

### Migration

0027 only widens the kind check; the single-kind unique index from 0022
already covers a new single-valued kind. The downgrade deletes the rows first.

## Not covered

Alert proposals (the Confirm step) show dollars only. Figures a model writes
from web results are not converted.
