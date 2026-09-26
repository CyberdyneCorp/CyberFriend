"""Self-service erasure: `erasure_request`, `person.erased_before`, the anonymous voice total.

`/privacy` -> [Delete everything...] removes everything held about a person,
with or without opting them out. This revision gives that flow what it needs
from the schema:

*   `erasure_request` is the durable record an erasure is resumed from. One
    row per request, `step` the last step that finished, `counts` what the
    reply reports. At most one open request per person. It holds numbers, not
    content, and it has to outlive the purge it drives, so
    `purge_person_derived` removes only a person's *completed* requests.

*   `person.erased_before` is how an erasure that keeps the person using the
    bot stops re-import. The message guard from 0008 now also drops a message
    whose author has `erased_before` later than the message's `created_at`,
    whatever issued the INSERT, exactly as it drops an opted-out author's. The
    same cut applies to the mention index (a mention of them in somebody
    else's earlier message is not indexed again) and to document entries they
    uploaded (dated by the Discord snowflake of the message, since an entry
    can land before its message row).

*   `media_usage_anonymous` keeps the server-wide monthly voice ceiling true
    once a person's `media_usage` rows are deleted: erasure folds their seconds
    in here, and the ceiling sums both tables. No person, no message, no text.

*   A trace whose Langfuse deletion was confirmed keeps only its id and
    `deleted_at`; the asker and the quoted-message links are cleared, and a
    finished asker search is deleted. Existing rows are scrubbed here, and
    the trace index does the same from now on, so an erasure leaves no record
    of when the person asked.

Revision ID: 0032
Revises: 0031
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

MODES = ("erase", "erase_and_opt_out")

#: `purge_person_derived` as 0030 left it. Restated rather than imported so this
#: revision stays what it was when applied, whatever a later one does.
PURGES_0030 = (
    "DELETE FROM conversation_turn WHERE person_id = p_person_id",
    "DELETE FROM conversation_summary WHERE person_id = p_person_id",
    "DELETE FROM person_fact WHERE person_id = p_person_id",
    "DELETE FROM notification WHERE person_id = p_person_id",
    "DELETE FROM position_alert WHERE person_id = p_person_id",
    "DELETE FROM scheduled_task WHERE person_id = p_person_id",
    """DELETE FROM mcp_token t
        USING person_platform_id p
        WHERE p.person_id = p_person_id
          AND t.platform = p.platform
          AND t.platform_user_id = p.platform_user_id""",
    "DELETE FROM feature_request WHERE person_id = p_person_id",
)

#: An open request is what a resumed erasure reads its next step from, so only
#: finished ones go.
PURGES = (
    *PURGES_0030,
    "DELETE FROM erasure_request WHERE person_id = p_person_id AND completed_at IS NOT NULL",
)

#: 0008's guard with the erasure cut added. The UPDATE exemption for a
#: withdrawal (`deleted_at` set) stays first: a withdrawal is never blocked.
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
    IF EXISTS (
        SELECT 1 FROM person
        WHERE id = NEW.author_person_id AND erased_before > NEW.created_at
    ) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

MESSAGE_GUARD_0008 = """
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

#: When Discord created a message, from its snowflake id (ms since 2015-01-01).
SNOWFLAKE_TIME = """
CREATE OR REPLACE FUNCTION discord_snowflake_time(p_id bigint) RETURNS timestamptz AS $$
    SELECT to_timestamp(((p_id >> 22) + 1420070400000) / 1000.0)
$$ LANGUAGE sql IMMUTABLE
"""

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
    IF NEW.uploader_person_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM person
        WHERE id = NEW.uploader_person_id
          AND erased_before > discord_snowflake_time(NEW.message_id)
    ) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

ENTRY_GUARD_0008 = """
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

#: The mention index points into somebody else's message, which stays. What
#: goes is the person-keyed path into it, and a backfill of that older message
#: must not put the path back.
MENTION_GUARD = """
CREATE OR REPLACE FUNCTION reject_erased_mention() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM person p JOIN message m ON m.id = NEW.message_id
        WHERE p.id = NEW.person_id AND p.erased_before > m.created_at
    ) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


#: A trace whose deletion Langfuse confirmed keeps its id and `deleted_at`
#: only (`CONFIRM_DELETED` does this from now on); rows confirmed before this
#: revision still name the asker and the quoted messages. Not undone on
#: downgrade: nothing reads them once the trace is gone.
SCRUB_CONFIRMED_TRACES = (
    "UPDATE trace_export SET asker_platform_user_id = NULL "
    "WHERE deleted_at IS NOT NULL AND asker_platform_user_id IS NOT NULL",
    "DELETE FROM trace_export_message tm USING trace_export t "
    "WHERE t.trace_id = tm.trace_id AND t.deleted_at IS NOT NULL",
)

#: A finished asker search is deleted rather than closed from now on.
SCRUB_FINISHED_SEARCHES = "DELETE FROM trace_asker_search WHERE completed_at IS NOT NULL"


def _purge_function(statements: tuple[str, ...]) -> str:
    return (
        "CREATE OR REPLACE FUNCTION purge_person_derived(p_person_id bigint) "
        "RETURNS void AS $$\nBEGIN\n"
        + "".join(f"    {statement};\n" for statement in statements)
        + "END;\n$$ LANGUAGE plpgsql"
    )


def upgrade() -> None:
    op.add_column("person", sa.Column("erased_before", sa.DateTime(timezone=True)))

    op.create_table(
        "erasure_request",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "person_id",
            sa.BigInteger,
            sa.ForeignKey("person.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # The last step that finished (1 = recorded, 8 = complete).
        sa.Column("step", sa.SmallInteger, nullable=False, server_default="1"),
        sa.Column(
            "counts",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # When a step last finished: the resume sweep leaves a request alone
        # while the process that started it is still making progress.
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "mode IN (" + ", ".join(f"'{m}'" for m in MODES) + ")",
            name="ck_erasure_request_mode",
        ),
        sa.CheckConstraint("step BETWEEN 1 AND 8", name="ck_erasure_request_step"),
    )
    op.create_index(
        "ux_erasure_request_open",
        "erasure_request",
        ["person_id"],
        unique=True,
        postgresql_where=sa.text("completed_at IS NULL"),
    )

    op.create_table(
        "media_usage_anonymous",
        sa.Column("month", sa.Date, primary_key=True),
        sa.Column("purpose", sa.Text, primary_key=True),
        sa.Column("seconds", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "purpose IN ('question', 'channel')", name="ck_media_usage_anonymous_purpose"
        ),
        sa.CheckConstraint("seconds >= 0", name="ck_media_usage_anonymous_seconds"),
    )

    op.execute(MESSAGE_GUARD)
    op.execute(SNOWFLAKE_TIME)
    op.execute(ENTRY_GUARD)
    op.execute(MENTION_GUARD)
    op.execute(
        "CREATE TRIGGER trg_message_mention_erased "
        "BEFORE INSERT ON message_mention "
        "FOR EACH ROW EXECUTE FUNCTION reject_erased_mention()"
    )
    op.execute(_purge_function(PURGES))
    for statement in SCRUB_CONFIRMED_TRACES:
        op.execute(statement)
    op.execute(SCRUB_FINISHED_SEARCHES)


def downgrade() -> None:
    op.execute(_purge_function(PURGES_0030))
    op.execute("DROP TRIGGER IF EXISTS trg_message_mention_erased ON message_mention")
    op.execute("DROP FUNCTION IF EXISTS reject_erased_mention()")
    op.execute(ENTRY_GUARD_0008)
    op.execute("DROP FUNCTION IF EXISTS discord_snowflake_time(bigint)")
    op.execute(MESSAGE_GUARD_0008)
    # The anonymous seconds have no person to go back to. Dropping them hands
    # the month's minutes back to the ceiling, which is the pre-0032 behaviour.
    op.drop_table("media_usage_anonymous")
    op.drop_table("erasure_request")
    op.drop_column("person", "erased_before")
