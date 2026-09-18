"""The queue between extracting an obligation and telling the person about it.

Revision ID: 0015
Revises: 0014
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# How a queued notification ended. Every row settles exactly once, and the
# reason is kept rather than the row deleted: "why did I never hear about
# this?" is the question this feature will actually be asked, and a deleted
# row answers it with silence.
#
#   sent           delivered to the person
#   unreadable     they could no longer read the source channel at send time
#   withdrawn      the ask was answered or corrected before it was sent
#   undeliverable  their direct messages are closed
#   expired        it sat in the queue past its shelf life
OUTCOMES = ("sent", "unreadable", "withdrawn", "undeliverable", "expired")


def upgrade() -> None:
    # Extraction runs in the ingest process; Discord is reachable only from
    # the bot. The two share no memory, so the queue is the seam -- the same
    # arrangement as the extraction watermark in 0010, where one pass records
    # what it has read so another can find what is left.
    #
    # Keyed by `ask_key`, not by an id of its own, and that is the whole
    # idempotence story: re-running extraction over the same message upserts
    # the same ask key, so the enqueue sweep can run as often as it likes and
    # a person is told about an obligation exactly once. A settled row stays
    # settled, so nothing is ever re-sent either.
    op.create_table(
        "notification",
        sa.Column(
            "ask_key",
            sa.Text,
            sa.ForeignKey("ask.ask_key", ondelete="CASCADE"),
            primary_key=True,
        ),
        # The addressee, and never anybody else. NOT NULL is the structural
        # half of "only the person an obligation names is notified": an ask
        # addressed to a group has no addressee person id, so a row for one
        # cannot be written at all.
        sa.Column(
            "person_id",
            sa.BigInteger,
            sa.ForeignKey("person.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalised from the ask for the same reason the ask denormalises
        # it from the message: the send-time permission re-check has to be a
        # predicate on this table's scan, not a filter over its output.
        sa.Column(
            "channel_id",
            sa.BigInteger,
            sa.ForeignKey("channel.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_message_id",
            sa.BigInteger,
            sa.ForeignKey("message.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.Text),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ("
            + ", ".join(f"'{o}'" for o in OUTCOMES)
            + ")",
            name="ck_notification_outcome",
        ),
        # A settled row says how it settled, and a pending one says nothing.
        # Without this a row could be marked sent with no record of whether it
        # reached anybody.
        sa.CheckConstraint(
            "(settled_at IS NULL) = (outcome IS NULL)",
            name="ck_notification_settled",
        ),
    )
    # The only shape either side queries: this person's pending rows, oldest
    # first. Partial, because the settled rows are history and the drain pass
    # runs every few seconds forever.
    op.create_index(
        "ix_notification_pending",
        "notification",
        ["person_id", "queued_at"],
        postgresql_where=sa.text("settled_at IS NULL"),
    )

    # Whether a person wants these at all, and what we have learned about
    # reaching them. Separate from the queue because it outlives every row in
    # it: "stop messaging me" must not be forgotten when the backlog drains.
    op.create_table(
        "notification_preference",
        sa.Column(
            "person_id",
            sa.BigInteger,
            sa.ForeignKey("person.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Default on, because a row only exists once somebody has been sent
        # something or has expressed a preference; absence means "never
        # considered", which for an unsolicited message is the same as on
        # only because every other bound -- addressee, opt-out, permission,
        # rate -- has already been applied by the time this is read.
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        # When we first messaged them. The first message has to say how to
        # stop, and "is this the first" must be a fact rather than a guess.
        sa.Column("first_notified_at", sa.DateTime(timezone=True)),
        # The rate limit, stored where the claim statement can read it: one
        # message per person per configured interval, enforced in SQL.
        sa.Column("last_notified_at", sa.DateTime(timezone=True)),
        # When delivery was last *attempted* and did not succeed. Separate
        # from the column above because the two answer different questions
        # and only one of them may be guessed at: "have we ever messaged
        # them" decides whether the next message says how to stop, and a
        # failed attempt is no evidence either way. The claim statement
        # applies the same interval to this, because a failed send may still
        # have put a message in front of the person -- a batch split into two
        # Discord messages whose second fails has already delivered its
        # first, and an accepted send whose response was lost is
        # indistinguishable from one that never arrived. Without it a person
        # whose delivery fails is re-selected on the next drain pass, seconds
        # later, and the only bound left is the queue's shelf life.
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        # Set when Discord refuses a direct message. Retrying a closed DM
        # forever is how a bot gets rate-limited into uselessness, and the
        # person has effectively already said no.
        sa.Column("undeliverable_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # The same exclusion 0008, 0013 and 0014 apply one table over. The claim
    # statements bind it as a predicate too; this is the guarantee, because a
    # future surface that writes this table by some other path still cannot
    # queue a message to somebody who withdrew.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_opted_out_notification() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM person_opt_out WHERE person_id = NEW.person_id
            ) THEN
                RETURN NULL;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_notification_opt_out "
        "BEFORE INSERT OR UPDATE ON notification "
        "FOR EACH ROW EXECUTE FUNCTION reject_opted_out_notification()"
    )

    # Opting out empties the queue in the same transaction as the flag, as
    # 0013 does for memory. The preference row is deliberately *not* removed:
    # somebody who turned notifications off and later opts back into indexing
    # has not asked to be messaged again, and dropping the row would quietly
    # re-enable them.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION purge_notifications_on_opt_out() RETURNS trigger AS $$
        BEGIN
            DELETE FROM notification WHERE person_id = NEW.person_id;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_person_opt_out_purges_notifications "
        "AFTER INSERT OR UPDATE ON person_opt_out "
        "FOR EACH ROW EXECUTE FUNCTION purge_notifications_on_opt_out()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_person_opt_out_purges_notifications ON person_opt_out"
    )
    op.execute("DROP FUNCTION IF EXISTS purge_notifications_on_opt_out()")
    op.drop_table("notification_preference")
    op.drop_index("ix_notification_pending", table_name="notification")
    op.drop_table("notification")
    op.execute("DROP FUNCTION IF EXISTS reject_opted_out_notification()")
