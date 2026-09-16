"""Console credentials, and the append-only record of what they changed.

Revision ID: 0012
Revises: 0010
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
# Chained to 0010, not to 0011. The stored-configuration tables are a separate
# piece of the same change and landed after this one, so 0011 revises 0012 and
# the chain runs 0010 -> 0012 -> 0011. The numbers are out of order and the
# order is what counts: a revision pointing at a parent that does not exist is
# not a placeholder, it is a migration that cannot run -- and a table that
# cannot be created is a feature that never runs.
down_revision = "0010"
branch_labels = None
depends_on = None

# The record is append-only, and the protocol in `admin/audit.py` cannot
# express anything else. That binds every caller that went through the port,
# which is every caller today and none of the ones added at a psql prompt
# during an incident. This trigger binds those too.
#
# It raises rather than silently dropping the write, unlike the opt-out guard
# in 0008: there, swallowing a row is the intended behaviour of an exclusion
# and an exception would stop ingestion for everybody in the channel. Here an
# attempted rewrite of the audit is never routine, and the caller must find
# out that it did not happen.
#
# TRUNCATE is not covered -- statement-level, and not reachable from the
# console -- which is deliberate: the test suite truncates every table
# between cases, and a record that made the suite unrunnable would be removed
# rather than kept.
APPEND_ONLY_GUARD = """
CREATE OR REPLACE FUNCTION reject_config_audit_rewrite() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'config_audit is append-only; % is not permitted', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "admin_token",
        # Only the hash. A database disclosure therefore yields nothing
        # replayable against the highest-privilege surface in the system.
        sa.Column("token_hash", sa.Text, primary_key=True),
        # The operator this credential acts as. Not nullable and not shared:
        # a credential that grants access without naming who holds it makes
        # the whole table below say "the token did it".
        sa.Column("operator", sa.Text, nullable=False),
        # Where this credential lives -- "ana's laptop". Free text, recorded
        # because "which one do I revoke" is asked later, under pressure.
        sa.Column("label", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Revocation is a tombstone: a withdrawn credential stays on record,
        # so "was this token ever valid, and when did it stop" stays
        # answerable after the fact.
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    # Partial, matching the only lookups that matter: authenticating a live
    # credential and revoking one operator's. Revoked rows are read by
    # neither.
    op.create_index(
        "ix_admin_token_operator",
        "admin_token",
        ["operator"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "config_audit",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Text, not a foreign key to admin_token.operator. The record has to
        # outlive the credential: revoking somebody's access must not be a
        # way to detach their name from what they changed.
        sa.Column("operator", sa.Text, nullable=False),
        sa.Column("setting", sa.Text, nullable=False),
        # 'applied', 'refused' or 'escalation'. Text rather than an enum type
        # so adding a kind is a code change rather than a migration on a
        # table that must never be rewritten.
        sa.Column("kind", sa.Text, nullable=False),
        # Nullable: an added setting has no before, a removed one has no
        # after, and a refusal may have neither.
        sa.Column("before_value", sa.Text),
        sa.Column("after_value", sa.Text),
        # Why a change was refused. The refusals are half the value of the
        # record: a boundary nobody can see being tested is a boundary
        # nobody maintains.
        sa.Column("reason", sa.Text),
    )
    # The console's audit view is "the newest entries", and the operator
    # question after an incident is "everything that touched this setting".
    op.create_index(
        "ix_config_audit_recorded_at", "config_audit", [sa.text("recorded_at DESC")]
    )
    op.create_index(
        "ix_config_audit_setting",
        "config_audit",
        ["setting", sa.text("recorded_at DESC")],
    )

    op.execute(APPEND_ONLY_GUARD)
    op.execute(
        "CREATE TRIGGER trg_config_audit_append_only "
        "BEFORE UPDATE OR DELETE ON config_audit "
        "FOR EACH ROW EXECUTE FUNCTION reject_config_audit_rewrite()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_config_audit_append_only ON config_audit")
    op.execute("DROP FUNCTION IF EXISTS reject_config_audit_rewrite()")
    op.drop_index("ix_config_audit_setting", table_name="config_audit")
    op.drop_index("ix_config_audit_recorded_at", table_name="config_audit")
    op.drop_table("config_audit")
    op.drop_index("ix_admin_token_operator", table_name="admin_token")
    op.drop_table("admin_token")
