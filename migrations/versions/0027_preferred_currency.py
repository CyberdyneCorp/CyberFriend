"""Preferred currency: the kind check widened with `preferred_currency`.

Single-valued, so the `ux_person_fact_single` index 0022 made already covers
it: stating a new currency replaces the old one. The value is an ISO 4217 code
the domain has validated; the column's existing length check is wider than any.

Revision ID: 0027
Revises: 0026
"""
from __future__ import annotations

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

BEFORE = (
    "preferred_name", "email", "preferred_language", "phone", "eth_wallet", "btc_wallet",
    "full_name", "home_address", "birth_date",
)
AFTER = (*BEFORE, "preferred_currency")


def _kinds(kinds: tuple[str, ...]) -> str:
    return ", ".join(f"'{k}'" for k in kinds)


def upgrade() -> None:
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
