# add-preferred-currency

A person can say which currency they want money shown in ("prefiro ver em
reais"), and every dollar figure the assistant reads for them -- prices,
balances, pools, Aave, portfolio totals, activity, alerts -- also shows in that
currency, converted in code at the daily ECB reference rate.

- `proposal.md` -- why, what changes, and the risk
- `design.md` -- the vocabulary, the rate lookup (migration 0027) and egress
- `specs/personal-facts/spec.md` -- the new fact kind
- `specs/preferred-currency/spec.md` -- where the second figure appears
