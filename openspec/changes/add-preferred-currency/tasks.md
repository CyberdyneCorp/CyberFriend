## 1. Model

- [x] 1.1 `preferred_currency` kind: closed vocabulary, aliases PT/EN, stored as the ISO code
- [x] 1.2 Migration 0027: kind check widened, downgrade drops the rows first

## 2. Recognition and replies

- [x] 2.1 Strict and loose statements, inside introductions too; show and forget
- [x] 2.2 Confirmation in the asker's language; USD means dollars only
- [x] 2.3 Unsupported currency refused with the supported list, nothing stored
- [x] 2.4 Labels, remember-list and self-description text (EN/PT)

## 3. Conversion

- [x] 3.1 `UsdRates` over the FX provider, ten-minute cache, never raises
- [x] 3.2 Market price tool result carries the converted figure and the rate
- [x] 3.3 Balances, positions, portfolio and activity renderers add the second figure
- [x] 3.4 Price and health alert messages use the owner's stored preference
- [x] 3.5 Asker context carries the code; synthesiser told to show both, never convert

## 4. Tests and docs

- [x] 4.1 Unit: aliases, intents, refusal, formatting, USD no-op, rate failure, cache
- [x] 4.2 Integration: store and downgrade
- [x] 4.3 End to end: introduction, price, portfolio, FX host down, /forget, listing
- [x] 4.4 README and docs/operations.md
