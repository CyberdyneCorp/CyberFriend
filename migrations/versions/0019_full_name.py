"""Full name, alongside the preferred name it is kept separate from.

Revision ID: 0019
Revises: 0018
"""
from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

BEFORE = (
    "preferred_name", "email", "preferred_language", "phone", "eth_wallet", "btc_wallet",
)
AFTER = (*BEFORE, "full_name")


def _kinds(kinds: tuple[str, ...]) -> str:
    return ", ".join(f"'{k}'" for k in kinds)


def upgrade() -> None:
    # Widening the constraint is the whole migration, as in 0018.
    op.drop_constraint("ck_person_fact_kind", "person_fact", type_="check")
    op.create_check_constraint(
        "ck_person_fact_kind", "person_fact", f"kind IN ({_kinds(AFTER)})"
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM person_fact WHERE kind NOT IN ({_kinds(BEFORE)})")
    op.drop_constraint("ck_person_fact_kind", "person_fact", type_="check")
    op.create_check_constraint(
        "ck_person_fact_kind", "person_fact", f"kind IN ({_kinds(BEFORE)})"
    )
