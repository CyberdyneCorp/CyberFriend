"""Phone and wallet addresses, alongside the facts already stored.

Revision ID: 0018
Revises: 0017
"""
from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

BEFORE = ("preferred_name", "email", "preferred_language")
AFTER = (*BEFORE, "phone", "eth_wallet", "btc_wallet")


def _kinds(kinds: tuple[str, ...]) -> str:
    return ", ".join(f"'{k}'" for k in kinds)


def upgrade() -> None:
    # The kind list is a constraint rather than a convention, so a kind the
    # code stops supporting cannot keep being written, and one it starts
    # supporting has to be added here deliberately. Widening it is the whole
    # migration: the table already holds one value per person per kind.
    op.drop_constraint("ck_person_fact_kind", "person_fact", type_="check")
    op.create_check_constraint(
        "ck_person_fact_kind", "person_fact", f"kind IN ({_kinds(AFTER)})"
    )


def downgrade() -> None:
    # Rows of the new kinds must go before the narrower constraint can hold.
    op.execute(f"DELETE FROM person_fact WHERE kind NOT IN ({_kinds(BEFORE)})")
    op.drop_constraint("ck_person_fact_kind", "person_fact", type_="check")
    op.create_check_constraint(
        "ck_person_fact_kind", "person_fact", f"kind IN ({_kinds(BEFORE)})"
    )
