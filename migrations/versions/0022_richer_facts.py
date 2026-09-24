"""Home address and birth date, and several wallets per person.

Two changes to `person_fact`:

*   The kind check is widened with `home_address` and `birth_date`.
*   Uniqueness is split by kind. A wallet kind is unique per (person, kind,
    value), so a person may save several addresses; every other kind stays
    unique per (person, kind), so setting it again still replaces it. Two
    partial unique indexes replace the one constraint, and the upserts in
    `facts_sql` name each index's predicate in `ON CONFLICT`.

The trigger 0020 put on `person_fact` is unchanged and already right for
several wallets: it fires per row and deletes only the saved-source alerts on
`OLD.value`, so forgetting one wallet removes that wallet's alerts and no
other's. What changes is that saving a second wallet no longer updates the
first row's value, so it no longer deletes the first wallet's alerts.

Revision ID: 0022
Revises: 0021
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

BEFORE = (
    "preferred_name", "email", "preferred_language", "phone", "eth_wallet", "btc_wallet",
    "full_name",
)
AFTER = (*BEFORE, "home_address", "birth_date")
WALLETS = ("eth_wallet", "btc_wallet")


def _kinds(kinds: tuple[str, ...]) -> str:
    return ", ".join(f"'{k}'" for k in kinds)


# Mirrored, character for character, by the `ON CONFLICT ... WHERE` clauses in
# `facts_sql`: Postgres infers a partial index only from a predicate that
# implies it.
SINGLE_PREDICATE = f"kind NOT IN ({_kinds(WALLETS)})"
WALLET_PREDICATE = f"kind IN ({_kinds(WALLETS)})"


def upgrade() -> None:
    op.drop_constraint("ck_person_fact_kind", "person_fact", type_="check")
    op.create_check_constraint(
        "ck_person_fact_kind", "person_fact", f"kind IN ({_kinds(AFTER)})"
    )
    op.drop_constraint("uq_person_fact_person_kind", "person_fact", type_="unique")
    op.create_index(
        "ux_person_fact_single",
        "person_fact",
        ["person_id", "kind"],
        unique=True,
        postgresql_where=sa.text(SINGLE_PREDICATE),
    )
    op.create_index(
        "ux_person_fact_wallet",
        "person_fact",
        ["person_id", "kind", "value"],
        unique=True,
        postgresql_where=sa.text(WALLET_PREDICATE),
    )


def downgrade() -> None:
    # Back to one wallet per kind: the most recently saved stays. The rows
    # deleted here fire 0020's trigger, so their saved-wallet alerts go too,
    # which is what forgetting a wallet has always meant.
    op.execute(f"DELETE FROM person_fact WHERE kind NOT IN ({_kinds(BEFORE)})")
    op.execute(
        f"""
        DELETE FROM person_fact f
         WHERE f.{WALLET_PREDICATE}
           AND EXISTS (
               SELECT 1 FROM person_fact newer
                WHERE newer.person_id = f.person_id
                  AND newer.kind = f.kind
                  AND (newer.updated_at, newer.id) > (f.updated_at, f.id)
           )
        """
    )
    op.drop_index("ux_person_fact_wallet", table_name="person_fact")
    op.drop_index("ux_person_fact_single", table_name="person_fact")
    op.create_unique_constraint(
        "uq_person_fact_person_kind", "person_fact", ["person_id", "kind"]
    )
    op.drop_constraint("ck_person_fact_kind", "person_fact", type_="check")
    op.create_check_constraint(
        "ck_person_fact_kind", "person_fact", f"kind IN ({_kinds(BEFORE)})"
    )
