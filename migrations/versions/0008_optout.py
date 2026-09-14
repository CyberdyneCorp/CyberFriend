"""Per-person opt-out, enforced by the database rather than by its callers.

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# The exclusion is enforced by a trigger, not by the ingestion service, and the
# reason is re-ingestion. Backfill re-reads history from the platform, which
# still holds every message a person asked us to forget. An application-level
# check would have to be remembered by live capture, by backfill, by
# reconciliation, and by every path added later; the one that forgets silently
# re-imports what the opt-out deleted, and nothing fails.
#
# A BEFORE INSERT trigger returning NULL skips the row without erroring, which
# is what "the message is not imported" has to look like to a bulk upsert: an
# exception would take down the whole page and stop ingestion for everybody
# else in the channel.
#
# UPDATE is covered too -- an edit must not be a way to write content for an
# excluded author -- with one exemption. Setting `deleted_at` is a withdrawal,
# and a withdrawal must never be blocked: if a purge fails halfway, the rows it
# left behind still have to be tombstonable.
MESSAGE_GUARD = """
CREATE OR REPLACE FUNCTION reject_opted_out_message() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.deleted_at IS NOT NULL THEN
        RETURN NEW;
    END IF;
    IF EXISTS (
        SELECT 1 FROM person_opt_out WHERE person_id = NEW.author_person_id
    ) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

# The same guard one corpus over. An opt-out that covers a person's messages
# and leaves the PDF they attached fully searchable has withdrawn the index
# entry and kept the content.
ENTRY_GUARD = """
CREATE OR REPLACE FUNCTION reject_opted_out_document_entry() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.deleted_at IS NOT NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.uploader_person_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM person_opt_out WHERE person_id = NEW.uploader_person_id
    ) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "person_opt_out",
        # Keyed on the canonical person, not on a platform account: somebody
        # with two Discord accounts opted out once, not once per account.
        sa.Column(
            "person_id",
            sa.BigInteger,
            sa.ForeignKey("person.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Free text, recorded because "why is this person's history missing"
        # is asked later and by someone who was not there.
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "opted_out_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute(MESSAGE_GUARD)
    op.execute(
        "CREATE TRIGGER trg_message_opt_out "
        "BEFORE INSERT OR UPDATE ON message "
        "FOR EACH ROW EXECUTE FUNCTION reject_opted_out_message()"
    )

    op.execute(ENTRY_GUARD)
    op.execute(
        "CREATE TRIGGER trg_document_entry_opt_out "
        "BEFORE INSERT OR UPDATE ON document_entry "
        "FOR EACH ROW EXECUTE FUNCTION reject_opted_out_document_entry()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_document_entry_opt_out ON document_entry")
    op.execute("DROP FUNCTION IF EXISTS reject_opted_out_document_entry()")
    op.execute("DROP TRIGGER IF EXISTS trg_message_opt_out ON message")
    op.execute("DROP FUNCTION IF EXISTS reject_opted_out_message()")
    op.drop_table("person_opt_out")
