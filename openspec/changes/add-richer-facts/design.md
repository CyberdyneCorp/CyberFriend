## Data model (migration 0022)

`person_fact` held one row per (person, kind), enforced by the unique
constraint `uq_person_fact_person_kind` that the upsert keyed on.

- The kind check is widened with `home_address` and `birth_date`.
- The constraint is replaced by two partial unique indexes:
  - `ux_person_fact_single (person_id, kind) WHERE kind NOT IN ('eth_wallet', 'btc_wallet')`
  - `ux_person_fact_wallet (person_id, kind, value) WHERE kind IN ('eth_wallet', 'btc_wallet')`
- `UPSERT_FACT` names the first predicate in `ON CONFLICT ... WHERE`; a new
  `ADD_WALLET` names the second and only refreshes `updated_at` on conflict,
  so re-saving a wallet is idempotent and never updates `value`.
- `FORGET_FACT_VALUE` deletes one value of one kind, keyed on the requester's
  platform identity like every other fact statement.

The cap (5 per wallet kind) is applied by `PersonalFactsService` before the
write; the store stays a plain add.

### Alerts on a saved wallet

0020's trigger fires per deleted row (or per update of `value`) of an
`eth_wallet` fact and deletes the saved-source alerts on `OLD.value`. It is
unchanged: with one row per wallet it already removes only the alerts on the
wallet that was forgotten. What changes is that saving a second wallet inserts
a row rather than updating the first, so the first wallet's alerts stay.

### Downgrade

Rows of the new kinds are deleted, then every wallet row but the most recent
per (person, kind) -- which fires the trigger, so their saved-wallet alerts go
too -- before the single unique constraint is restored.

## Privacy

| Kind | Shown in a channel | In a channel prompt | Channel message archived |
|---|---|---|---|
| home_address | never | never | withheld |
| birth_date | never | never | withheld |
| eth/btc wallets | never | never | archived, as before |

In a DM the prompt carries `home_address`, `birth_date` and `eth_wallets` /
`btc_wallets` as lists (so five addresses are never cut by the per-field cap).
The egress guard's authorised values (`asker_values`) hold every saved wallet;
the address and birth date never leave. A reply that asks which wallet lists
only the last four characters of each.
