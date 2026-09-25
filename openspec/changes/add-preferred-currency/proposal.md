## Why

Every figure the assistant reads is in US dollars: CoinGecko quotes BTC and ETH
in dollars, and the chain readers value pools and Aave positions with dollar
oracles. Most of the server thinks in reais. "Qual o preço do bitcoin?" was
answered "63,210 USD", and the person had to convert it themselves -- or ask
the conversion tool a second question.

## What Changes

- **A new personal fact, `preferred_currency`.** An ISO 4217 code from a closed
  set: the currencies Frankfurter publishes an ECB reference rate for. Stated
  as "minha moeda é o real brasileiro", "my currency is euro", "moeda
  preferida: BRL", "prefiro ver em reais", or "uso reais" inside an
  introduction; names are read in Portuguese and English. An unsupported
  currency (the Argentine peso, dogecoin) is refused with the supported list,
  and nothing is stored. US dollars as the preference means no second figure.
  Shown by "o que você sabe sobre mim", removed by `/forget` and by "esqueça
  minha moeda", replaced by stating another. Not private: shown in a channel
  like the preferred language.
- **A second figure beside every dollar figure.** With a preference saved, the
  BTC/ETH price, wallet balances, liquidity and lending positions, the
  portfolio total, wallet activity and price/health alert messages show each
  dollar amount followed by the same amount in that currency, written in the
  reader's notation ("US$ 63.210,00 (R$ 345.126,60)"), with a footnote naming
  the rate. Converted in code; the market answer carries the converted figure
  in the tool result, and the synthesiser is told to show both and never to
  convert.
- **One rate per answer.** USD to the preferred code, read through the market
  FX provider and `Edges.http_transport` from the host the conversion tool
  already uses, and cached in-process for ten minutes. A rate that cannot be
  read leaves the answer in dollars alone.
- **Migration 0027** widens `ck_person_fact_kind` with `preferred_currency`.

## Capabilities

### New Capabilities

- `preferred-currency`: the second figure, where it appears, and the rate.

### Modified Capabilities

- `personal-facts`: the `preferred_currency` kind.

## Impact

- Egress: one new request shape to an existing host,
  `api.frankfurter.dev/v1/latest?from=USD&to=<code>`, made without a
  question's clearance. The code is a member of the closed set and nothing
  else about the person is sent.
- Migration 0027, chained after 0026 (`feat/media-capture`).

## Risk

"uso X" and "prefiro ver em X" are statements only when X is a currency name,
so "uso o Discord" and "prefiro ver em português" are untouched. A bare code
counts only in capitals. The confirmation names the currency back, so a
misrecognised statement is visible at once, and the preference is one sentence
away from being forgotten.
